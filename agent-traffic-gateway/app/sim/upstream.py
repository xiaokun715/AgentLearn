"""上游 LLM 的仿真 —— §2.1 容量模型的可执行版本。

这个模块只干一件事：**忠实地按「并发槽位 × 服务时间」的规则消耗请求**，
从而让「容量 = 并发 / 服务时间」这句公式在仿真里成为事实，
而不是我们假设出来的。

三个必须显式建模的行为（它们都是 §3.5「上游是外部依赖」的具体化）：

1. **并发槽位有限** —— 超过就排队，排太深就 429；
2. **服务时间有方差** —— 对数正态，§3.1 说的「P99/P50 差 50 倍」从这里来；
3. **会抖动** —— ``transient_fail_rate`` 是 §12.1 重试放大器的起点。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..domain.models import UpstreamConfig
from .rng import Rng


@dataclass
class UpstreamCall:
    """一次上游调用的生命周期。"""

    call_id: str
    request_id: str
    submitted_ms: int
    ok: bool = True
    service_ms: int = 0
    started_ms: int = 0
    finished_ms: int = 0
    #: 该调用是否携带一个降级标记（§13.2 切小模型）
    degraded: bool = False
    #: 服务时间系数：降级时 < 1（切小模型），用于 §13.2 的 brownout
    speed_factor: float = 1.0

    @property
    def wait_ms(self) -> int:
        """在上游内部的排队时间 —— 这一段是**你看不见的延迟**（§2.4）。"""
        return max(0, self.started_ms - self.submitted_ms)


@dataclass
class UpstreamStats:
    submitted: int = 0
    accepted: int = 0
    rejected_429: int = 0
    completed: int = 0
    failed: int = 0
    service_ms_total: int = 0
    #: 每个 tick 采样的在途数与内部队列深度，用于画时间序列
    inflight_samples: list[int] = field(default_factory=list)
    queue_samples: list[int] = field(default_factory=list)
    peak_queue: int = 0
    peak_inflight: int = 0

    @property
    def mean_service_ms(self) -> float:
        return self.service_ms_total / self.completed if self.completed else 0.0


#: 上游完成时的回调签名：``(call, ok, now_ms)``
UpstreamSink = Callable[[UpstreamCall, bool, int], None]


class UpstreamLLM:
    """模拟的 LLM 供应商。"""

    def __init__(
        self,
        config: UpstreamConfig,
        rng: Rng,
        sink: UpstreamSink,
    ) -> None:
        self.config = config
        self.rng = rng
        self.sink = sink
        self.stats = UpstreamStats()
        self._queue: deque[UpstreamCall] = deque()
        self._active: list[UpstreamCall] = []

    # ------------------------------------------------------------------
    # §2.2 Little's Law 的两个直接观测量
    # ------------------------------------------------------------------
    @property
    def inflight(self) -> int:
        return len(self._active)

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def available_slots(self) -> int:
        return max(0, self.config.max_concurrency - len(self._active))

    # ------------------------------------------------------------------
    def submit(self, call: UpstreamCall, now_ms: int) -> bool:
        """提交一次上游调用。返回 ``False`` 表示**被上游 429 拒绝**。

        §3.5：「必须假设上游的容量小于你的接入能力」。这个 ``False``
        就是网关存在的理由 —— 如果没有网关做准入，被 429 的打回
        只会在客户端侧变成重试，然后更糟。
        """
        self.stats.submitted += 1
        call.submitted_ms = now_ms
        call.ok = not self.rng.bernoulli(self.config.transient_fail_rate)

        if len(self._queue) >= self.config.internal_queue_limit:
            self.stats.rejected_429 += 1
            return False

        self._queue.append(call)
        self.stats.accepted += 1
        self.stats.peak_queue = max(self.stats.peak_queue, len(self._queue))
        return True

    # ------------------------------------------------------------------
    def tick(self, now_ms: int, *, sample: bool = False) -> None:
        """推进一个 tick：先释放槽位，再补位。"""
        # ① 完成到期的调用。放在补位之前，是为了让「刚空出来的槽位」
        #    在同一个 tick 内就能被利用，不浪费一个 tick 的吞吐。
        still_active: list[UpstreamCall] = []
        for call in self._active:
            if call.finished_ms <= now_ms:
                call.finished_ms = now_ms
                self.stats.completed += 1
                self.stats.service_ms_total += call.finished_ms - call.started_ms
                if not call.ok:
                    self.stats.failed += 1
                self.sink(call, call.ok, now_ms)
            else:
                still_active.append(call)
        self._active = still_active

        # ② 补位
        while self._queue and len(self._active) < self.config.max_concurrency:
            call = self._queue.popleft()
            call.started_ms = now_ms
            call.service_ms = self._draw_service_ms(call, now_ms)
            call.finished_ms = now_ms + call.service_ms
            self._active.append(call)

        self.stats.peak_inflight = max(self.stats.peak_inflight, len(self._active))
        if sample:
            self.stats.inflight_samples.append(len(self._active))
            self.stats.queue_samples.append(len(self._queue))

    # ------------------------------------------------------------------
    def _draw_service_ms(self, call: UpstreamCall, now_ms: int) -> int:
        mean = float(self.config.mean_service_ms) * max(call.speed_factor, 1e-6)
        if (
            self.config.slow_down_at_ms is not None
            and now_ms >= self.config.slow_down_at_ms
            and self.config.slow_down_factor != 1.0
        ):
            # 上游劣化的时刻。注意这是**服务时间**变长，不是到达率变化 ——
            # 所以按 QPS 扩容的 HPA 完全不会响应（§14.1 的经典失败）。
            mean *= self.config.slow_down_factor
        value = self.rng.lognormal(mean, self.config.service_jitter)
        return max(1, int(value))

    # ------------------------------------------------------------------
    def mark_slow_down(self, factor: float) -> None:
        """运行时改变上游速度（用于演示「容量突然缩水」）。"""
        self.config.slow_down_at_ms = 0
        self.config.slow_down_factor = factor
