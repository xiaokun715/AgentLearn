"""准入控制与分级降级（说明书 §7 / §13）。

这是整章最重要的一个设计点，也是最容易被做错的一个。

**「队列满了就拒绝」是错的**（§7.1）——它把一个**时间**问题当成了
**空间**问题。队列长度相同的情况下：上游空闲时这 200 个请求 20ms 就排完了
（拒绝是误伤），上游很慢时它们要 40 秒（全部会超时，拒绝才对）。

所以正确的判据是比较两个时间（§7.2）：

    预计等待 = 队列深度 / 有效吞吐率
    预计完成 = now + 预计等待 + 服务时间估计
    可行     = 预计完成 <= deadline

它有一个很漂亮的推论：**过载时它会自动优先拒绝长请求**，
而长请求恰好是最该被牺牲的（它占用槽位最久）。

## 为什么丢弃边界用「排队压力」而不是「并发利用率」

这是实现过程中实测出来的一个错误。第一版用 ``inflight / limit`` 当阈值，
结果发现**信号量一满就开始丢请求** —— 可「并发槽位全满」恰恰是设计工作点，
不是过载。一个健康系统在稳态下利用率就接近 1，于是它拒掉了 5% 的正常流量。

正确的量是无量纲的**排队压力**：

    压力 = 预计排队时间 / 单次服务时间

它有两个好处：与并发数无关（换个部署不用重调阈值），
而且直接对应 §16.3 里那个「雪崩前最有价值的信号」——
「排队已经超过计算」。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..domain.enums import Priority, RejectReason, ShedLevel
from ..domain.models import GatewayConfig, Request


class ThroughputEstimator:
    """§7.3 有效吞吐率的估计。

    两种估计**取较小值**：

    - **静态容量** ``并发上限 / 平均服务时间``：稳态下准确，但上游变慢时
      **过于乐观**（上游慢两倍，真实吞吐掉一半，而它还在按旧速率放行）；
    - **滑动窗口实测**：统计最近 N 毫秒的**完成数**，自动包含上游变慢的影响，
      但有滞后 —— 上游快速劣化的头几秒它还反应不过来。

    取较小值是一个保守的折中：宁可少放行几个，也不要放行注定超时的请求。
    """

    def __init__(
        self,
        window_ms: int,
        static_capacity_per_ms: float,
        min_samples: int = 5,
    ) -> None:
        self.window_ms = window_ms
        self.static_capacity_per_ms = static_capacity_per_ms
        self.min_samples = min_samples
        self._completions: deque[int] = deque()
        self._started_ms: int | None = None

    def observe_completion(self, now_ms: int) -> None:
        self._completions.append(now_ms)

    def estimate_per_ms(self, now_ms: int) -> float:
        """当前有效吞吐（请求数 / 毫秒）。

        **未预热时必须退回静态容量**，否则会有一个很尴尬的冷启动 bug：
        系统刚起来的头几百毫秒，窗口里只有一两个完成记录，
        实测值会被算成 0.3 RPS，于是准入控制认为「什么都来不及」，
        把前几秒的**所有**请求全部拒绝。

        这不是保守，这是自我实现的预言 —— 拒绝得越多，完成得越少，
        估计值越低，然后拒绝得更多。

        所以：窗口没满 或 样本太少 → 用静态容量；两者都满足才采信实测。
        """
        if self._started_ms is None:
            self._started_ms = now_ms
        cutoff = now_ms - self.window_ms
        while self._completions and self._completions[0] <= cutoff:
            self._completions.popleft()

        warmed = (now_ms - self._started_ms) >= self.window_ms
        if not warmed or len(self._completions) < self.min_samples:
            return self.static_capacity_per_ms

        observed_per_ms = len(self._completions) / self.window_ms
        return min(self.static_capacity_per_ms, observed_per_ms)


@dataclass
class AdmissionDecision:
    """一次准入裁决。

    ``predicted_wait_ms`` 与 ``pressure`` 都要能被打印出来 ——
    「为什么拒绝我」必须可回答（§7.4）。
    """

    allow: bool
    reason: RejectReason | None = None
    shed_level: ShedLevel = ShedLevel.NORMAL
    predicted_wait_ms: int = 0
    predicted_finish_ms: int = 0
    #: 排队压力 = 预计排队时间 / 服务时间（§16.3）
    pressure: float = 0.0
    degrade: bool = False
    rationale: str = ""


class AdmissionController:
    """§7 准入 + §13 分级降级。"""

    def __init__(
        self,
        config: GatewayConfig,
        estimator: ThroughputEstimator,
        concurrency_limit: int,
    ) -> None:
        self.config = config
        self.estimator = estimator
        self.concurrency_limit = concurrency_limit
        self.rejected_infeasible = 0
        self.rejected_overloaded = 0
        self.degraded = 0
        #: 最近一次裁决的压力，供时间序列与终端展示
        self.last_pressure = 0.0
        self.last_shed_level = ShedLevel.NORMAL

    # ------------------------------------------------------------------
    def utilisation(self, inflight: int) -> float:
        """ρ：在途请求数 / 并发上限（§2.3 利用率悬崖里的 ρ）。

        **它只用于观测，不用于决策** —— 决策用的是压力。原因是利用率
        在有了信号量之后会长期贴着 1.0，无法区分「刚好跑满」和「已经排爆」。
        """
        return inflight / self.concurrency_limit if self.concurrency_limit else 1.0

    def shed_level(self, pressure: float) -> ShedLevel:
        """§13.1 按**排队压力**决定丢弃边界。"""
        if not self.config.shedding_enabled:
            return ShedLevel.NORMAL
        if pressure >= self.config.shed_critical_pressure:
            return ShedLevel.CRITICAL
        if pressure >= self.config.shed_hard_pressure:
            return ShedLevel.HARD
        if pressure >= self.config.shed_soft_pressure:
            return ShedLevel.SOFT
        return ShedLevel.NORMAL

    # ------------------------------------------------------------------
    def decide(
        self,
        req: Request,
        now_ms: int,
        *,
        queue_depth: int,
        inflight: int,
        cache_hit: bool = False,
    ) -> AdmissionDecision:
        # 先算压力，再用同一个压力同时驱动「丢弃」与「降级」——
        # 两个决策共用一个判据，才不会出现「在降级却没在丢弃」这种自相矛盾的状态。
        throughput = self.estimator.estimate_per_ms(now_ms)
        predicted_wait = int((queue_depth + 1) / throughput) if throughput > 0 else 0
        pressure = predicted_wait / max(self.config.service_time_estimate_ms, 1)
        level = self.shed_level(pressure)
        self.last_pressure = pressure
        self.last_shed_level = level

        # ---- §7.2 用「预计完成时间」而不是「队列长度」 -------------------
        #
        # **这一步刻意放在分级丢弃之前**，理由是信息量与代价：
        #
        #   · 代价相同（两个判据用的是同一批已经算好的数）；
        #   · 但可行性是关于**这一个请求**的精确陈述，而丢弃是关于
        #     **整个系统**的粗略陈述。先给出精确的那一个，
        #     调用方拿到的就是「你的请求来不及了」（可行动），
        #     而不是「系统忙」（只能盲目重试）。
        #
        # 早先版本把丢弃放在前面，实测结果是 DEADLINE_INFEASIBLE
        # 一次都没触发过 —— 每条注定超时的请求都先在 HARD/CRITICAL
        # 丢弃那里被拦下了，于是 §7.4 那条「拒绝要给出可行动的信息」
        # 在代码里成立、在行为上却失效。
        predicted_finish = now_ms + predicted_wait + req.expected_service_ms

        if self.config.admission_enabled and predicted_finish > req.deadline_ms:
            self.rejected_infeasible += 1
            return AdmissionDecision(
                allow=False,
                reason=RejectReason.DEADLINE_INFEASIBLE,
                shed_level=level,
                predicted_wait_ms=predicted_wait,
                predicted_finish_ms=predicted_finish,
                pressure=pressure,
                rationale=(
                    f"预计完成 +{predicted_finish - now_ms}ms 超过剩余预算 "
                    f"{req.deadline_ms - now_ms}ms（排队 {predicted_wait}ms + "
                    f"服务 {req.expected_service_ms}ms；吞吐估计 "
                    f"{throughput * 1000:.2f} RPS）"
                ),
            )

        # ---- §13.1 分级丢弃：先丢批处理与低优先级 ----------------------
        if level is not ShedLevel.NORMAL:
            if req.priority.value < Priority.INTERACTIVE.value and level is ShedLevel.HARD:
                self.rejected_overloaded += 1
                return AdmissionDecision(
                    allow=False,
                    reason=RejectReason.OVERLOADED,
                    shed_level=level,
                    predicted_wait_ms=predicted_wait,
                    pressure=pressure,
                    rationale=(
                        f"排队压力 {pressure:.1f} ≥ {self.config.shed_hard_pressure}，"
                        f"只保留 INTERACTIVE 及以上（本请求 {req.priority.name}）"
                    ),
                )
            if level is ShedLevel.CRITICAL and not cache_hit:
                self.rejected_overloaded += 1
                return AdmissionDecision(
                    allow=False,
                    reason=RejectReason.OVERLOADED,
                    shed_level=level,
                    predicted_wait_ms=predicted_wait,
                    pressure=pressure,
                    rationale=(
                        f"排队压力 {pressure:.1f} ≥ {self.config.shed_critical_pressure}，"
                        "只放行缓存命中"
                    ),
                )
            if level is ShedLevel.SOFT and req.priority is Priority.BATCH:
                self.rejected_overloaded += 1
                return AdmissionDecision(
                    allow=False,
                    reason=RejectReason.OVERLOADED,
                    shed_level=level,
                    predicted_wait_ms=predicted_wait,
                    pressure=pressure,
                    rationale=(
                        f"排队压力 {pressure:.1f} ≥ {self.config.shed_soft_pressure}，"
                        "先丢批处理"
                    ),
                )

        # ---- §13.2 Brownout：降级而不是拒绝 ----------------------------
        # 只有「本来会被服务」的请求才降级；降级不能让结果变错、只能变差，
        # 所以它必须在响应里显式标注（由调用方决定接不接受）。
        degrade = (
            self.config.degrade_enabled
            and pressure >= self.config.degrade_from_pressure
            and req.priority is not Priority.BATCH
        )
        if degrade:
            self.degraded += 1

        return AdmissionDecision(
            allow=True,
            shed_level=level,
            predicted_wait_ms=predicted_wait,
            predicted_finish_ms=predicted_finish,
            pressure=pressure,
            degrade=degrade,
        )

    def snapshot(self) -> dict:
        return {
            "rejected_infeasible": self.rejected_infeasible,
            "rejected_overloaded": self.rejected_overloaded,
            "degraded": self.degraded,
            "last_pressure": round(self.last_pressure, 3),
            "shed_level": self.last_shed_level.value,
        }
