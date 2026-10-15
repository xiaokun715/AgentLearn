"""流量生成 —— 让「大流量」这个词变成一个可复现的输入。

三种真实流量的特征被显式建模：

1. **泊松到达**（指数间隔）。不是均匀间隔 —— 均匀间隔是实验室里才有的东西，
   它对系统的压力显著小于同样均值的泊松到达；
2. **Zipf 热点**。真实流量几乎总是 Zipf（搜索、推荐、「帮我总结一下」）。
   这一条决定了缓存与合并的价值上限，所以它必须是流量的属性，
   而不是一个「命中率」参数；
3. **突发**。突发只能靠削峰处理，不能靠扩容（§17 第 5 步）。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import TenantSpec, WorkloadSpec
from .rng import Rng


@dataclass
class Arrival:
    """一个待发生的请求（还没变成 :class:`~app.domain.models.Request`）。"""

    at_ms: int
    tenant: TenantSpec
    key_index: int
    variant: int
    index: int


class Workload:
    """把 :class:`WorkloadSpec` 编译成一串确定性的到达事件。"""

    def __init__(self, spec: WorkloadSpec, rng: Rng) -> None:
        self.spec = spec
        self.rng = rng
        self.arrivals: list[Arrival] = []
        self._zipf_weights: dict[int, list[float]] = {}
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        spec = self.spec
        # ① 先算出所有到达时刻（这一步只消耗指数分布的随机数）
        timed: list[tuple[int, TenantSpec]] = []
        for tenant in spec.tenants:
            for at in self._poisson_stream(tenant.rate_rps, 0, spec.duration_ms):
                timed.append((at, tenant))
            # ② 突发：在突发窗口内**额外**加一条 (factor-1) 倍的流。
            #    用「额外的流」而不是「改速率」，是为了让突发和不突发
            #    使用同一个基础种子 —— 对照实验比较的才是突发本身。
            if spec.burst_at_ms is not None and spec.burst_factor > 1.0:
                extra = tenant.rate_rps * (spec.burst_factor - 1.0)
                window_end = min(
                    spec.duration_ms, spec.burst_at_ms + spec.burst_duration_ms
                )
                for at in self._poisson_stream(extra, spec.burst_at_ms, window_end):
                    timed.append((at, tenant))

        timed.sort(key=lambda item: item[0])

        # ③ 按**时间顺序**采样 key 与变体。
        #    顺序很关键：先排序再抽样，同一份流量跑两次的 key 序列才完全相同。
        for index, (at, tenant) in enumerate(timed):
            key = self._sample_key(tenant)
            variant = 1 if (tenant.variant_ratio > 0 and self.rng.bernoulli(tenant.variant_ratio)) else 0
            self.arrivals.append(
                Arrival(at_ms=at, tenant=tenant, key_index=key, variant=variant, index=index)
            )

    # ------------------------------------------------------------------
    def _poisson_stream(self, rate_rps: float, start_ms: int, end_ms: int) -> list[int]:
        """泊松过程的一条实现（指数间隔）。"""
        if rate_rps <= 0:
            return []
        mean_gap_ms = 1000.0 / rate_rps
        times: list[int] = []
        cursor = float(start_ms)
        while True:
            cursor += self.rng.exponential(mean_gap_ms)
            if cursor >= end_ms:
                break
            times.append(int(cursor))
        return times

    def _sample_key(self, tenant: TenantSpec) -> int:
        """Zipf 抽样：第 i 个 key 的权重 ∝ 1/(i+1)^α。"""
        weights = self._zipf_weights.get(tenant.prompt_space)
        if weights is None:
            alpha = self.spec.zipf_alpha
            weights = [1.0 / ((i + 1) ** alpha) for i in range(tenant.prompt_space)]
            self._zipf_weights[tenant.prompt_space] = weights
        return self.rng.weighted_index(weights)

    # ------------------------------------------------------------------
    @property
    def total_requests(self) -> int:
        return len(self.arrivals)

    def stats(self) -> dict:
        """流量的画像：真实系统里这几个数应该来自线上采样。"""
        spec = self.spec
        by_tenant: dict[str, int] = {}
        for item in self.arrivals:
            by_tenant[item.tenant.tenant_id] = by_tenant.get(item.tenant.tenant_id, 0) + 1
        duration_s = spec.duration_ms / 1000.0
        return {
            "duration_ms": spec.duration_ms,
            "total_requests": self.total_requests,
            "achieved_rps": round(self.total_requests / duration_s, 2),
            "by_tenant": by_tenant,
            "zipf_alpha": spec.zipf_alpha,
            "burst": (
                f"{spec.burst_factor}× @{spec.burst_at_ms}ms"
                if spec.burst_at_ms is not None else None
            ),
            "distinct_prompt_space": {
                t.tenant_id: t.prompt_space for t in spec.tenants
            },
        }
