"""仿真内核 —— 把上面所有部件按一个确定的顺序推起来。

**tick 内部的阶段顺序是刻意固定的**，而且它本身就是一条设计结论：

    ① 上游 tick（释放槽位、完成/失败回调）
    ② 到达（生成新请求，走进网关管线）
    ③ 网关 tick（超期清扫 → 派发到上游）
    ④ 采样

为什么把「上游完成」放在「到达」之前：槽位应该**先释放再接收新工作**。
反过来的话，一个刚空出来的槽位要等到下一个 tick 才能被利用 ——
在高负载下这会凭空吃掉一份吞吐，而且这个损失和 tick 大小成正比（假象）。

**关于随机数**：这里刻意用了两个独立的 RNG。

    workload_rng  只负责生成流量（种子来自 spec）
    runtime_rng   负责服务时间抖动、TTL 抖动、退避抖动

原因是做 A/B 对照实验（例如「同一批流量，只换调度策略」）时，
两边的**到达序列必须完全相同**，否则你比较的是流量的差异而不是策略的差异。
把流量 RNG 独立出来之后，「同一个种子 → 同一批请求」是结构性成立的。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.models import (
    GatewayConfig,
    Request,
    UpstreamConfig,
    WorkloadSpec,
    make_request,
)
from ..gateway.gateway import Gateway
from ..observe.metrics import Metrics
from .autoscaler import Autoscaler
from .clock import SimClock
from .rng import Rng
from .upstream import UpstreamLLM
from .workload import Workload


@dataclass
class SimResult:
    """一次仿真的完整产出。"""

    duration_ms: int
    metrics: Metrics
    gateway: dict = field(default_factory=dict)
    upstream: dict = field(default_factory=dict)
    autoscaler: dict = field(default_factory=dict)
    workload: dict = field(default_factory=dict)
    config: GatewayConfig | None = None
    upstream_config: UpstreamConfig | None = None

    # ---- 便捷读数（终端与断言都直接用这些）----------------------------
    @property
    def summary(self) -> dict:
        return self.metrics.summary(self.duration_ms)

    @property
    def p50(self) -> int:
        return self.metrics.latency.percentile(0.50)

    @property
    def p99(self) -> int:
        return self.metrics.latency.percentile(0.99)

    @property
    def success_ratio(self) -> float:
        return self.metrics.succeeded / self.metrics.total if self.metrics.total else 0.0

    @property
    def upstream_calls(self) -> int:
        return self.metrics.upstream_calls

    @property
    def achieved_rps(self) -> float:
        return self.metrics.succeeded / (self.duration_ms / 1000.0)

    def series(self, name: str) -> list[float]:
        return self.metrics.timeline.get(name, [])


class Simulation:
    """确定性离散时间仿真。"""

    def __init__(
        self,
        config: GatewayConfig,
        upstream_config: UpstreamConfig,
        workload_spec: WorkloadSpec,
        *,
        tick_ms: int = 1,
        sample_interval_ms: int = 100,
    ) -> None:
        self.config = config
        self.upstream_config = upstream_config
        self.workload_spec = workload_spec
        self.tick_ms = tick_ms
        self.sample_interval_ms = sample_interval_ms

        self.clock = SimClock(tick_ms)
        # 两个 RNG：流量一条、运行时一条（见模块 docstring）
        self.workload_rng = Rng(workload_spec.seed)
        self.runtime_rng = Rng(workload_spec.seed + 1)

        self.workload = Workload(workload_spec, self.workload_rng)
        self.metrics = Metrics(sample_interval_ms=sample_interval_ms)
        self.upstream = UpstreamLLM(upstream_config, self.runtime_rng, self._on_upstream)
        self.gateway = Gateway(
            config, self.upstream, self.runtime_rng, self.metrics.record
        )
        self.autoscaler = Autoscaler(config)
        self._arrival_index = 0
        self._pending_at_end = 0

    # ------------------------------------------------------------------
    def _on_upstream(self, call, ok: bool, now_ms: int) -> None:
        """上游回调 —— 直接转交给网关（网关是唯一知道怎么处置请求的地方）。"""
        self.gateway.on_upstream_done(call, ok, now_ms)

    # ------------------------------------------------------------------
    def run(self) -> SimResult:
        arrivals = self.workload.arrivals
        total_ticks = self.workload_spec.duration_ms // self.tick_ms

        for _ in range(total_ticks):
            now_ms = self.clock.advance()

            # ① 上游推进：先释放槽位
            sample_now = (now_ms % self.sample_interval_ms) == 0
            self.upstream.tick(now_ms, sample=sample_now)

            # ② 到达
            while (
                self._arrival_index < len(arrivals)
                and arrivals[self._arrival_index].at_ms <= now_ms
            ):
                self._admit(arrivals[self._arrival_index], now_ms)
                self._arrival_index += 1

            # ③ 网关：清扫 → 派发
            self.gateway.tick(now_ms)

            # ④ 弹性伸缩（§14）
            if self.config.autoscale_enabled:
                self._autoscale(now_ms)

            # ⑤ 采样
            if sample_now:
                self.metrics.sample(
                    now_ms,
                    queue_depth=self.gateway.scheduler.depth,
                    inflight=self.gateway.semaphore.inflight,
                    limit=self.gateway.semaphore.limit,
                    shed_level=self.gateway.current_shed_level(),
                    upstream_inflight=self.upstream.inflight,
                    upstream_queue=self.upstream.queue_depth,
                    pressure=self.gateway.admission.last_pressure,
                )

        # 收尾：未完成的请求也要如实计入（否则统计会系统性偏乐观）
        self._pending_at_end = self.gateway.drain_pending(self.clock.now_ms)
        # 放大比要看**真实提交次数**（含重试），它只能从网关计数器拿
        self.metrics.upstream_submissions = self.gateway.counters.upstream_calls
        self.metrics.retry_submissions = self.gateway.counters.retries

        return SimResult(
            duration_ms=self.workload_spec.duration_ms,
            metrics=self.metrics,
            gateway=self.gateway.snapshot(),
            upstream={
                "capacity_rps": round(self.upstream_config.capacity_rps, 3),
                "submitted": self.upstream.stats.submitted,
                "completed": self.upstream.stats.completed,
                "failed": self.upstream.stats.failed,
                "rejected_429": self.upstream.stats.rejected_429,
                "mean_service_ms": round(self.upstream.stats.mean_service_ms, 1),
                "peak_queue": self.upstream.stats.peak_queue,
                "peak_inflight": self.upstream.stats.peak_inflight,
                "pending_at_end": self._pending_at_end,
            },
            autoscaler=self.autoscaler.snapshot(),
            workload=self.workload.stats(),
            config=self.config,
            upstream_config=self.upstream_config,
        )

    # ------------------------------------------------------------------
    def _admit(self, arrival, now_ms: int) -> None:
        req: Request = make_request(
            request_id=f"r{arrival.index}",
            tenant=arrival.tenant,
            key_index=arrival.key_index,
            variant=arrival.variant,
            now_ms=now_ms,
            budget_ms=self.config.request_budget_ms,
            service_ms=self.config.service_time_estimate_ms,
        )
        self.gateway.handle(req, now_ms)

    def _autoscale(self, now_ms: int) -> None:
        # 观测窗口内的实际 QPS：注意它来自**完成数**而不是到达数，
        # 这样它反映的是「系统真正服务了多少」，而不是「被要求服务多少」。
        window_s = max(self.sample_interval_ms, 1000) / 1000.0
        qps = self.metrics.succeeded / max(self.clock.now_ms / 1000.0, 1.0)
        desired = self.autoscaler.decide(
            now_ms,
            queue_depth=self.gateway.scheduler.depth,
            qps=qps,
            utilisation=self.gateway.semaphore.utilisation,
            current_limit=self.gateway.semaphore.limit,
        )
        if desired is not None:
            self.gateway.semaphore.set_limit(desired)
        _ = window_s
