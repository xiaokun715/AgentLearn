"""默认策略与流量画像 —— 全章的「数字」都集中在这里。

这一层的存在理由是说明书 §18 的最后一条反模式：

> **限流阈值写死在代码里** —— 出故障时改不动（需要发版），失去最后的手段。

所以：阈值只在 :class:`~app.domain.models.GatewayConfig` 里定义一次，
本模块只负责给出**有意义的默认值**与**可复现的流量画像**，
再由 ``configs/gateway.yaml`` 覆盖。任何地方都不允许出现裸的数字。
"""
from __future__ import annotations

from .enums import Priority
from .models import GatewayConfig, TenantSpec, UpstreamConfig, WorkloadSpec

# ======================================================================
# §2.1 上游容量：全章一切计算的起点
# ======================================================================
#: 8 个并发槽 × 1.5 秒平均服务时间 = **5.33 RPS** 的稳态容量。
#: 这个数会在 README 与场景输出里反复出现，因为它就是「容量天花板」。
DEFAULT_UPSTREAM = UpstreamConfig(
    max_concurrency=8,
    mean_service_ms=1500,
    service_jitter=0.45,
    internal_queue_limit=48,
)


def upstream_capacity_rps(config: UpstreamConfig = DEFAULT_UPSTREAM) -> float:
    """§2.1 ``容量 = 并发 / 平均服务时间``。"""
    return config.capacity_rps


def target_rps(utilization: float, config: UpstreamConfig = DEFAULT_UPSTREAM) -> float:
    """按目标利用率反推到达率。

    §2.3 的结论是目标水位应该取 60%~70%，不是 90%。这个函数就是那句话的算子：
    ``target_rps(0.65)`` 得到的才是「这个部署能长期稳定服务的流量」。
    """
    return upstream_capacity_rps(config) * utilization


# ======================================================================
# 租户画像
# ======================================================================
def tenant(
    tenant_id: str,
    rate_rps: float,
    *,
    weight: float = 1.0,
    priority: Priority = Priority.STANDARD,
    prompt_space: int = 200,
    batch: bool = False,
    variant_ratio: float = 0.0,
) -> TenantSpec:
    return TenantSpec(
        tenant_id=tenant_id,
        rate_rps=rate_rps,
        weight=weight,
        priority=priority,
        prompt_space=prompt_space,
        batch=batch,
        variant_ratio=variant_ratio,
    )


#: 单租户标准画像：命中率由 Zipf(prompt_space) 决定
SINGLE_TENANT = lambda rate, **kw: [  # noqa: E731 - 场景里当短别名用，语义明确
    tenant("acme", rate, **kw)
]


# ======================================================================
# §8.3 公平性场景的画像：一大一小两个租户
# ======================================================================
#: 大租户 10 RPS、小租户 0.1 RPS。两者的差别是 100 倍 ——
#: 这个比例不是随手取的：它要大到能让 FIFO 的不公平现象稳定复现。
#:
#: 注意 10 RPS 远超容量 5.33 RPS，所以队列**必然**堆积，
#: 而堆积的时候 FIFO 会让小租户排在 100 倍流量的后面。
FAIRNESS_TENANTS = [
    tenant("whale", 12.0, weight=1.0, priority=Priority.STANDARD),
    # 0.5 RPS 而不是 0.1：40 秒里 0.1 RPS 只有约 4 个请求，
    # 样本量太小，P99 根本没有统计意义（它会是「某一个请求的延迟」）。
    tenant("mouse", 0.5, weight=1.0, priority=Priority.STANDARD),
]


#: §13.1 的丢弃顺序画像：付费交互、普通、批处理三种质的流量混在一起
MIXED_TENANTS = [
    tenant("interactive", 4.0, priority=Priority.INTERACTIVE),
    tenant("standard", 3.0, priority=Priority.STANDARD),
    tenant("batch", 3.0, priority=Priority.BATCH, batch=True),
]


# ======================================================================
# 流量构造
# ======================================================================
def steady_workload(
    rate_rps: float,
    *,
    duration_ms: int = 60_000,
    prompt_space: int = 200,
    zipf_alpha: float = 1.1,
    variant_ratio: float = 0.5,
    seed: int = 20261016,
    **tenant_kw,
) -> WorkloadSpec:
    """恒定到达率的单租户流量。

    ``variant_ratio`` 默认 0.5：同一个意图（``semantic_key``）有一半概率以
    **另一种表面形式**出现。这样精确缓存与语义缓存才会各自产生有意义的、
    且互不相同的命中率（§10.1）。取 1.0 会让所有请求都是变体、取 0 会让两层
    完全重合 —— 两者都看不出这一层的价值。
    """
    return WorkloadSpec(
        duration_ms=duration_ms,
        zipf_alpha=zipf_alpha,
        seed=seed,
        tenants=[tenant("acme", rate_rps, prompt_space=prompt_space,
                        variant_ratio=variant_ratio, **tenant_kw)],
    )


def burst_workload(
    base_rps: float,
    *,
    burst_at_ms: int = 20_000,
    duration_ms: int = 60_000,
    burst_duration_ms: int = 3000,
    burst_factor: float = 6.0,
    **kwargs,
) -> WorkloadSpec:
    """§17 第 5 步的突发：**不要指望用机器吸收突发**。

    机器的扩容速度是分钟级，突发是秒级的 —— 所以突发只能靠
    削峰（排队 + 准入）和合并处理，不能靠扩容。
    """
    spec = steady_workload(base_rps, duration_ms=duration_ms, **kwargs)
    return spec.model_copy(
        update={
            "burst_at_ms": burst_at_ms,
            "burst_duration_ms": burst_duration_ms,
            "burst_factor": burst_factor,
        }
    )


def multi_tenant_workload(
    tenants: list[TenantSpec],
    *,
    duration_ms: int = 60_000,
    seed: int = 20261016,
) -> WorkloadSpec:
    return WorkloadSpec(duration_ms=duration_ms, tenants=tenants, seed=seed)


# ======================================================================
# 配置预设
# ======================================================================
def governed(**overrides) -> GatewayConfig:
    """§21 阶段二~四之后的完整治理配置。"""
    return GatewayConfig(**overrides)


def ungoverned(**overrides) -> GatewayConfig:
    """§20 ``baseline`` 的对照组：一个治理手段都不开。"""
    return GatewayConfig().without_governance().with_(**overrides)
