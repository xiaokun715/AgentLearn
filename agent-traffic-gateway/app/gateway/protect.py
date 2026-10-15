"""上游保护 —— 并发信号量 / 重试预算 / 熔断器（说明书 §11 / §12）。

三个组件对应三种不同的「保护上游」的机制，不能互相替代：

==================================== ==========================================
:class:`UpstreamSemaphore`           把容量变成一个明确的数（§11.1）
:class:`RetryBudget`                 防止重试自我放大（§12.2）
:class:`CircuitBreaker`              上游整体故障时快速失败（§12.4）
==================================== ==========================================

其中**重试预算是本 Demo 最有价值的一段** —— 因为它是雪崩的主要放大器（§12.1）：

    E[请求数] = (1 - p^n) / (1 - p)

失败率 80%、重试 3 次时，负载被放大 **2.44 倍**；而负载放大又让失败率更高。
正反馈，这就是雪崩的机制。重试预算把「重试次数」换成
「重试占总请求的比例」，于是它**永远不会自我放大**。
"""
from __future__ import annotations

from collections import deque

from ..domain.enums import CircuitState
from ..domain.models import GatewayConfig


class UpstreamSemaphore:
    """§11.1 并发信号量 —— Little's Law 里 ``L`` 的那个显式上限。

    ``limit`` 的确定方式：**不能超过上游配额**（否则等着收 429），
    **不能太高**（否则排队发生在你看不见的地方，观测失真）。
    经验起点是配额的 80%，再按 P99 和队列深度调。
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.inflight = 0
        self.acquired_total = 0
        self.denied_total = 0
        #: 每个采样点的在途数，用于画「饱和度」曲线
        self.samples: list[int] = []

    def try_acquire(self) -> bool:
        if self.inflight >= self.limit:
            self.denied_total += 1
            return False
        self.inflight += 1
        self.acquired_total += 1
        return True

    def release(self) -> None:
        if self.inflight > 0:
            self.inflight -= 1

    def set_limit(self, limit: int) -> None:
        """§14 弹性伸缩在运行时调的就是这个数。"""
        self.limit = max(1, limit)

    @property
    def utilisation(self) -> float:
        return self.inflight / self.limit if self.limit else 1.0

    def sample(self) -> None:
        self.samples.append(self.inflight)


class RetryBudget:
    """§12.2 Google SRE 式的重试预算。

    机制：**每成功一个请求补充 ``ratio`` 个令牌，每次重试消耗 1 个令牌。**

    于是「重试数 ≤ ratio × 成功数」是结构上成立的，而不是靠调用方自觉。
    这个性质在过载时极其关键：流量越大，允许的绝对重试数越多，
    但它**永远只占请求的一小部分**，所以不会自我放大。

    ``ratio=None`` 表示无预算（朴素重试）—— 这个开关是 ``resilience``
    场景的对照组，用来量化「没有预算会糟多少」。
    """

    def __init__(self, ratio: float | None, burst: int = 20, initial: float = 5.0) -> None:
        self.ratio = ratio
        self.burst = float(burst)
        self.tokens = initial
        self.denied = 0
        self.granted = 0

    def on_success(self) -> None:
        if self.ratio is None:
            return
        self.tokens = min(self.burst, self.tokens + self.ratio)

    def allow(self) -> bool:
        if self.ratio is None:
            self.granted += 1
            return True       # 无预算：朴素重试，想重试就重试
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            self.granted += 1
            return True
        self.denied += 1
        return False

    @property
    def saturation(self) -> float:
        return 1.0 - (self.tokens / self.burst if self.burst else 0.0)

    def snapshot(self) -> dict:
        total = self.granted + self.denied
        return {
            "ratio": self.ratio,
            "tokens": round(self.tokens, 2),
            "granted": self.granted,
            "denied": self.denied,
            "deny_ratio": round(self.denied / total, 4) if total else 0.0,
        }


class CircuitBreaker:
    """§12.4 熔断器。

    三个参数都要想清楚，任何一个设错都会让它变成负担：

    - **统计窗口**：太短会误判（一次抖动就熔断），太长会反应迟钝；
    - **最少样本数**：低流量时不能用比例判断（1 次失败 ≠ 100% 失败率）；
    - **冷却时间**：要和上游恢复时间同量级，太短会「熔断-恢复-熔断」反复抖。

    熔断的**代价**是它会拒绝掉本来可能成功的请求，所以它只应在
    「失败率已经高到重试明显不划算」时打开。
    """

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.state = CircuitState.CLOSED
        self._events: deque[tuple[int, bool]] = deque()
        self.opened_ms: int | None = None
        self.half_open_probes = 0
        self.trips = 0
        self.short_circuited = 0

    # ------------------------------------------------------------------
    def allow(self, now_ms: int) -> bool:
        if not self.config.breaker_enabled:
            return True

        if self.state is CircuitState.OPEN:
            if (
                self.opened_ms is not None
                and now_ms - self.opened_ms >= self.config.breaker_cooldown_ms
            ):
                self.state = CircuitState.HALF_OPEN
                self.half_open_probes = 0
            else:
                self.short_circuited += 1
                return False

        if self.state is CircuitState.HALF_OPEN:
            if self.half_open_probes >= self.config.breaker_half_open_probes:
                self.short_circuited += 1
                return False
            return True

        return True

    def record(self, ok: bool, now_ms: int) -> None:
        if not self.config.breaker_enabled:
            return

        if self.state is CircuitState.HALF_OPEN:
            self.half_open_probes += 1
            if not ok:
                self._open(now_ms)
                return
            if self.half_open_probes >= self.config.breaker_half_open_probes:
                self.state = CircuitState.CLOSED
                self._events.clear()
            return

        self._events.append((now_ms, ok))
        cutoff = now_ms - self.config.breaker_window_ms
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()

        if len(self._events) < self.config.breaker_min_samples:
            return
        failures = sum(1 for _, ok_ in self._events if not ok_)
        ratio = failures / len(self._events)
        if ratio > self.config.breaker_failure_ratio:
            self._open(now_ms)

    def _open(self, now_ms: int) -> None:
        if self.state is not CircuitState.OPEN:
            self.trips += 1
        self.state = CircuitState.OPEN
        self.opened_ms = now_ms
        self.half_open_probes = 0

    # ------------------------------------------------------------------
    @property
    def failure_ratio(self) -> float:
        if not self._events:
            return 0.0
        return sum(1 for _, ok in self._events if not ok) / len(self._events)

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "trips": self.trips,
            "short_circuited": self.short_circuited,
            "failure_ratio": round(self.failure_ratio, 4),
            "samples_in_window": len(self._events),
        }


class RetryPlanner:
    """§12.3 退避与抖动。

    **必须加抖动**。不加抖动的指数退避会让所有失败的请求在同一时刻
    一起重试 —— 同步的重试风暴比不重试更糟，因为它制造了人为的尖峰。

    而且退避要**结合 deadline**：如果 ``backoff > 剩余时间``，
    直接失败，不要睡（用 §11.2 的话说：超时预算不守恒的典型表现）。
    """

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.suppressed_by_deadline = 0
        self.suppressed_by_budget = 0

    def backoff_ms(self, rng, attempt: int) -> int:
        raw = self.config.retry_base_ms * (2 ** attempt)
        capped = min(self.config.retry_cap_ms, raw)
        # 全抖动（full jitter）：取 [0, capped] 而不是 [capped/2, capped]
        return int(rng.uniform(0.0, capped))

    def should_retry(self, attempt: int, now_ms: int, deadline_ms: int, backoff: int) -> bool:
        if attempt + 1 >= self.config.retry_max_attempts:
            return False
        if now_ms + backoff >= deadline_ms:
            self.suppressed_by_deadline += 1
            return False
        return True

    def snapshot(self) -> dict:
        return {
            "max_attempts": self.config.retry_max_attempts,
            "suppressed_by_deadline": self.suppressed_by_deadline,
            "suppressed_by_budget": self.suppressed_by_budget,
        }
