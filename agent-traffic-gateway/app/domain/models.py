"""流量治理领域模型 —— 请求、结果、以及**全部的治理阈值**。

对齐《第十六章：高并发大流量 Agent 网关设计说明书》：
- §5.1 请求                 -> :class:`Request`
- §8.2 优先级与老化          -> :class:`Request.effective_priority`
- §16  结果与指标            -> :class:`OutcomeRecord`
- §6~§14 全部治理参数        -> :class:`GatewayConfig`
- §2.1 上游容量模型          -> :class:`UpstreamConfig`
- §3.5 上游会抖动            -> :class:`UpstreamConfig.transient_fail_rate`
- 场景流量                   -> :class:`WorkloadSpec`
"""
from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field, model_validator

from .enums import (
    Outcome,
    Priority,
    QueuePolicy,
    RejectReason,
    ServedBy,
)


# ======================================================================
# 三、§3.2 —— 请求
# ======================================================================
class Request(BaseModel):
    """一个进入网关的请求。

    ``cache_key`` 与 ``semantic_key`` 是两个不同的东西，这一点很容易被混淆：

    - ``cache_key``：**精确**指纹（模型 + 规范化消息 + 采样参数 + 工具集）。
      少一个维度就会串结果，所以它必须覆盖所有影响输出的输入（§9.2）；
    - ``semantic_key``：**意图**指纹。「把这段话翻译成英文」和
      「翻译成英文：把这段话」语义相同，精确指纹不同，但语义指纹相同。

    精确缓存的上限是「完全相同请求的占比」，语义缓存的上限是
    「意图相同请求的占比」—— 后者才是命中率的真正天花板（§10.1）。
    """

    request_id: str
    tenant_id: str
    priority: Priority = Priority.STANDARD
    cache_key: str
    semantic_key: str
    arrived_ms: int
    deadline_ms: int
    #: 该租户在公平调度里的权重（§8.4 WFQ）
    tenant_weight: float = 1.0
    #: 重试次数（首次为 0）
    attempts: int = 0
    #: 是否由重试产生 —— 重试请求入队时要降优先级（§5 的两个易错点）
    is_retry: bool = False
    #: 上游对该请求服务时间的期望值（用于准入控制的预计完成时间计算）
    expected_service_ms: int = 1500
    #: 是否允许被合并（§9.2：带副作用 / 带独立计费语义的不能合并）
    coalescable: bool = True
    #: 准入阶段就定下来的降级标记 —— 它必须跟着请求穿过队列，
    #: 否则「这一刻该降级」这个判断到真正派发时就已经过期了
    degrade: bool = False

    @property
    def remaining_ms(self) -> int:
        return self.deadline_ms - self.arrived_ms

    def effective_priority(self, now_ms: int, aging_per_second: float) -> float:
        """§8.2 有效优先级 = 基础优先级 + 老化。

        老化的存在是为了解决纯优先级队列的致命缺陷：**低优先级饿死**。
        高优先级流量持续不断时，低优先级永远排不到 —— 老化让
        「等得够久」本身变成一种优先级。
        """
        waited_s = max(0, now_ms - self.arrived_ms) / 1000.0
        return self.priority.value + aging_per_second * waited_s


# ======================================================================
# §16 结果
# ======================================================================
class OutcomeRecord(BaseModel):
    """一次请求的最终处置。**这是所有指标的原始事实。**"""

    request_id: str
    tenant_id: str
    priority: Priority
    status: Outcome
    reason: RejectReason | None = None
    served_by: ServedBy = ServedBy.UPSTREAM
    latency_ms: int = 0
    queue_ms: int = 0
    attempts: int = 0
    degraded: bool = False
    arrived_ms: int = 0
    finished_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (Outcome.OK, Outcome.DEGRADED)

    @property
    def upstream_call(self) -> bool:
        """这次请求**是否真的消耗了一次上游调用**（重试算多次）。

        §12.1 的「负载放大」就是拿 ``upstream_call`` 的总数除以请求总数。
        """
        return self.served_by is ServedBy.UPSTREAM


# ======================================================================
# §6 ~ §14 全部治理阈值
# ======================================================================
class GatewayConfig(BaseModel):
    """网关的热配置。

    §18 的最后一条反模式是「限流阈值写死在代码里」—— 故障时改不动阈值
    等于把最后的阀门焊死了。所以本 Demo 把**所有**阈值集中在这一处，
    并且允许 ``configs/gateway.yaml`` 覆盖（见 ``factory.load_config``）。
    """

    # ---- §6 多级限流 --------------------------------------------------
    edge_rps: float = 200.0            # L1 全局边缘
    edge_burst: int = 400
    tenant_rps: dict[str, float] = Field(default_factory=dict)   # L2 每租户
    tenant_burst: dict[str, int] = Field(default_factory=dict)

    # ---- §8 队列与调度 ------------------------------------------------
    queue_capacity: int = 64
    queue_policy: QueuePolicy = QueuePolicy.PRIORITY_AGING
    aging_per_second: float = 0.5      # 老化权重：等 2 秒等价于提升一级
    tenant_weights: dict[str, float] = Field(default_factory=dict)

    # ---- §11.1 并发控制 -----------------------------------------------
    upstream_concurrency: int = 8      # 信号量上限：§2.2 Little's Law 里的 L

    # ---- §7 准入控制 --------------------------------------------------
    admission_enabled: bool = True
    #: 吞吐估计的安全系数（保守一点：宁可少放行，也别放行注定超时的）
    admission_safety: float = 0.9
    #: 吞吐估计的滑动窗口（§7.3：静态容量在稳态好、上游变慢时过于乐观）
    throughput_window_ms: int = 3000

    # ---- §11.2 超时预算 -----------------------------------------------
    #: 端到端预算：只有一个绝对 deadline，每层用「剩余时间」
    request_budget_ms: int = 9000
    #: 是否清扫已经超过 deadline 的排队请求（§11.2 的 18% 算力就来自这里）
    sweep_expired: bool = True
    #: 清扫周期。真实系统也不会每毫秒扫一遍队列 —— 扫一次是 O(队列长度)，
    #: 这里把它显式写出来，是因为「清扫多久一次」直接决定释放多少无效算力。
    sweep_interval_ms: int = 50
    #: 服务时间估计（用于准入控制里「预计完成」的第二项）
    service_time_estimate_ms: int = 1500

    # ---- §10 分层缓存 -------------------------------------------------
    l1_capacity: int = 2048
    l2_capacity: int = 16384
    cache_ttl_ms: int = 60_000
    #: TTL 抖动（§10.2：固定 TTL 会在整点周期性制造缓存雪崩）
    cache_ttl_jitter: float = 0.25
    semantic_cache_enabled: bool = True

    # ---- §9 请求合并 --------------------------------------------------
    coalescing_enabled: bool = True

    # ---- §12 重试与熔断 -----------------------------------------------
    retry_enabled: bool = True
    retry_max_attempts: int = 3        # 含首次
    #: §12.2 重试预算：重试数占总成功数的比例上限。``None`` = 无预算（朴素重试）
    retry_budget_ratio: float | None = 0.10
    retry_base_ms: int = 200
    retry_cap_ms: int = 2000

    breaker_enabled: bool = True
    breaker_window_ms: int = 10_000
    breaker_min_samples: int = 20
    breaker_failure_ratio: float = 0.5
    breaker_cooldown_ms: int = 5_000
    breaker_half_open_probes: int = 3

    # ---- §13 过载保护与降级 -------------------------------------------
    #: 「分级丢弃」与「降级」是两个独立开关。把它们绑在一个字段上，
    #: 就会在「只想量一下裸容量」的时候意外触发丢弃 —— 测出来的不是容量，
    #: 而是「被丢弃之后的容量」。
    shedding_enabled: bool = True
    degrade_enabled: bool = True
    # 丢弃边界用的是**排队压力**，不是「并发利用率」：
    #
    #     压力 = 预计排队时间 / 单次服务时间
    #
    # 用利用率是错的 —— 有了信号量之后，「并发槽位全满」是**设计工作点**，
    # 不是过载。拿 inflight/limit 当阈值，会让系统在正常稳态下就疯狂丢弃
    # （这一点在 Demo 里实测过：利用率口径下，一个健康系统会拒掉 5% 的请求）。
    # 而排队压力是无量纲的、与并发数无关的，它直接对应 §16.3 里那个
    # 「最有价值的饱和信号」。
    #
    # 这三个阈值不能随手取 —— 它们必须与 request_budget_ms 自洽，
    # 否则会有一条判据永远触发不到（factory.check_config 会检查这件事）：
    #
    #     可行性判据在「预计完成 > deadline」时触发，即
    #         排队时间 > budget - 服务时间
    #     如果 critical 阈值比这更小，那么每条注定超时的请求都会先在
    #     CRITICAL 丢弃那里被拦下，DEADLINE_INFEASIBLE **一次都不会出现**。
    #     两个判据都对，但调用方拿到的原因就永远是笼统的「系统忙」。
    shed_soft_pressure: float = 1.0        # 排队时间 ≈ 服务时间
    shed_hard_pressure: float = 2.5        # 排队时间 ≈ 2.5 倍服务时间
    shed_critical_pressure: float = 6.0    # 排队时间 ≈ 6 倍服务时间
    #: 从哪个压力开始降级（§13.2 brownout）
    degrade_from_pressure: float = 0.8
    #: 降级后的服务时间系数（切小模型：§13.2 表格里的 ×0.3）
    degrade_service_factor: float = 0.35

    # ---- §14 弹性伸缩 -------------------------------------------------
    autoscale_enabled: bool = False
    autoscale_signal: str = "QUEUE_DEPTH"   # CPU / QPS / QUEUE_DEPTH
    autoscale_queue_threshold: int = 24
    autoscale_cold_start_ms: int = 5000     # §14.2 冷启动
    autoscale_step: int = 4                 # 每次扩容增加的并发槽位

    @model_validator(mode="after")
    def _check_consistency(self) -> GatewayConfig:
        if self.shed_soft_pressure >= self.shed_hard_pressure:
            raise ValueError("shed_soft_pressure 必须小于 shed_hard_pressure")
        if self.shed_hard_pressure >= self.shed_critical_pressure:
            raise ValueError("shed_hard_pressure 必须小于 shed_critical_pressure")
        if self.request_budget_ms <= self.service_time_estimate_ms:
            raise ValueError("端到端预算必须大于单次服务时间估计，否则所有请求都不可行")
        return self

    # ------------------------------------------------------------------
    def with_(self, **overrides) -> GatewayConfig:
        """返回一份打了补丁的副本（场景切换用，避免互相污染）。"""
        return self.model_copy(update=overrides)

    def without_governance(self) -> GatewayConfig:
        """§20 ``baseline`` 场景：把所有治理手段关掉，作为对照组。"""
        return self.with_(
            admission_enabled=False,
            sweep_expired=False,
            queue_capacity=100_000,        # 等价于无界队列（§18 第一大反模式）
            coalescing_enabled=False,
            semantic_cache_enabled=False,
            l1_capacity=0,
            l2_capacity=0,
            retry_enabled=False,
            breaker_enabled=False,
            shedding_enabled=False,
            degrade_enabled=False,
            upstream_concurrency=10_000,   # 不设信号量：并发由流量决定
        )


# ======================================================================
# §2.1 上游
# ======================================================================
class UpstreamConfig(BaseModel):
    """上游 LLM 的容量模型。

    三个参数决定了**你的容量上限**（§2.1），所以它们必须在仿真里是显式的：

    - ``max_concurrency``：供应商允许的并发槽位（这是天花板）；
    - ``mean_service_ms``：平均服务时间（容量 = 并发 / 服务时间）；
    - ``transient_fail_rate``：瞬时失败率（§12.1 的重试放大器从这里开始）。
    """

    max_concurrency: int = 8
    mean_service_ms: int = 1500
    #: 对数正态的 sigma —— §3.1「服务时间方差极大」的量化
    service_jitter: float = 0.45
    #: 内部排队上限；超过则返回 429（模拟真实供应商的行为）
    internal_queue_limit: int = 48
    #: 瞬时失败率（0.25 用于演示重试风暴）
    transient_fail_rate: float = 0.0
    #: 上游变慢开始时刻（用于演示「上游劣化导致容量缩水」）
    slow_down_at_ms: int | None = None
    slow_down_factor: float = 1.0

    @property
    def capacity_rps(self) -> float:
        """§2.1 容量 = 并发 / 平均服务时间。"""
        return self.max_concurrency / (self.mean_service_ms / 1000.0)


# ======================================================================
# 流量场景
# ======================================================================
class TenantSpec(BaseModel):
    """一个租户的流量画像。"""

    tenant_id: str
    rate_rps: float
    weight: float = 1.0
    priority: Priority = Priority.STANDARD
    #: 该租户的 prompt 空间大小（越小越热，命中率越高）
    prompt_space: int = 200
    #: 是否批处理租户（§13.1 过载时第一个被丢）
    batch: bool = False
    #: 该租户的语义变体比例（§10.1：语义缓存能多命中的部分）
    variant_ratio: float = 0.0


class WorkloadSpec(BaseModel):
    """一条流量曲线。"""

    duration_ms: int = 60_000
    #: Zipf 指数：真实流量几乎总是 Zipf（搜索、推荐、「帮我总结一下」）
    zipf_alpha: float = 1.1
    tenants: list[TenantSpec] = Field(default_factory=list)
    #: 突发：在第 N 毫秒开始，持续 M 毫秒，速率乘以 K
    burst_at_ms: int | None = None
    burst_duration_ms: int = 3000
    burst_factor: float = 6.0
    seed: int = 20261016

    @property
    def total_rate_rps(self) -> float:
        return sum(t.rate_rps for t in self.tenants)


def make_request(
    *,
    request_id: str,
    tenant: TenantSpec,
    key_index: int,
    variant: int,
    now_ms: int,
    budget_ms: int,
    service_ms: int,
) -> Request:
    """按租户画像生成一个请求（含精确指纹与语义指纹）。"""
    base = f"q{key_index}"
    cache_key = hashlib.sha256(
        f"{base}|model=gpt-4o|temp=0|tools=on".encode()
    ).hexdigest()[:16]
    if variant:
        cache_key = hashlib.sha256(
            f"{base}|variant={variant}|model=gpt-4o|temp=0|tools=on".encode()
        ).hexdigest()[:16]
    return Request(
        request_id=request_id,
        tenant_id=tenant.tenant_id,
        priority=tenant.priority,
        cache_key=cache_key,
        semantic_key=base,
        arrived_ms=now_ms,
        deadline_ms=now_ms + budget_ms,
        tenant_weight=tenant.weight,
        expected_service_ms=service_ms,
        coalescable=not tenant.batch,
    )
