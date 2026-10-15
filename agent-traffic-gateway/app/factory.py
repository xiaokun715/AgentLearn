"""装配层 —— 把配置、流量与仿真内核接起来，并做启动自检。

对应说明书 §19 的项目结构，以及 §18「常见反模式」的可执行版本：
:func:`check_config` 把那些反模式逐条变成启动时的检查。

**为什么要区分 problems 与 notices**：本 Demo 的很多场景**故意**关掉某些治理
手段（那是对照组），所以「重试无预算」在 demo 里是合法的、在生产上是危险的。
把两者混成一个「有问题」的告警，团队就会开始忽略所有告警。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .domain.enums import QueuePolicy, UpgradeSignal
from .domain.models import GatewayConfig, UpstreamConfig, WorkloadSpec
from .sim.engine import Simulation

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "gateway.yaml"


# ======================================================================
# 配置加载
# ======================================================================
def load_config(path: str | Path | None = DEFAULT_CONFIG_PATH) -> GatewayConfig:
    """代码默认值 + YAML 覆盖（§18 最后一条：阈值必须是运行时可改的）。

    YAML 只能覆盖**已存在**的字段 —— 拼错的键会直接报错，而不是被静默忽略。
    静默忽略一个阈值配置，等于让运维以为自己改了但实际上没改。
    """
    config = GatewayConfig()
    if path is None:
        return config
    file = Path(path)
    if not file.exists():
        return config
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    unknown = set(raw) - set(GatewayConfig.model_fields)
    if unknown:
        raise KeyError(f"configs/gateway.yaml 里存在未知阈值：{sorted(unknown)}")
    return config.model_copy(update=raw)


# ======================================================================
# 装配
# ======================================================================
def build_simulation(
    config: GatewayConfig,
    upstream: UpstreamConfig,
    workload: WorkloadSpec,
    *,
    tick_ms: int = 1,
    sample_interval_ms: int = 100,
) -> Simulation:
    return Simulation(
        config, upstream, workload,
        tick_ms=tick_ms, sample_interval_ms=sample_interval_ms,
    )


# ======================================================================
# 启动自检（§18 反模式的可执行版本）
# ======================================================================
@dataclass
class ConfigReport:
    problems: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_config(
    config: GatewayConfig,
    upstream: UpstreamConfig | None = None,
) -> ConfigReport:
    """检查配置是否自洽。"""
    report = ConfigReport()

    # ---- 1. 并发上限不能超过上游配额（§11.1）---------------------------
    if upstream is not None and config.upstream_concurrency > upstream.max_concurrency:
        report.problems.append(
            f"upstream_concurrency={config.upstream_concurrency} 超过上游配额 "
            f"{upstream.max_concurrency} —— 多出来的部分会变成上游侧的 429，"
            "排队发生在你看不见的地方（§2.4）"
        )

    # ---- 2. 端到端预算必须大于单次服务时间（§11.2）---------------------
    if config.request_budget_ms <= config.service_time_estimate_ms:
        report.problems.append(
            f"request_budget_ms={config.request_budget_ms} 不大于服务时间估计 "
            f"{config.service_time_estimate_ms} —— 所有请求都会被判为不可行，"
            "准入控制会拒绝掉全部流量"
        )

    # ---- 3. 队列容量不能小于并发上限（§8.1）----------------------------
    if config.queue_capacity < config.upstream_concurrency:
        report.problems.append(
            f"queue_capacity={config.queue_capacity} 小于并发上限 "
            f"{config.upstream_concurrency} —— 信号量永远填不满，"
            "有界队列变成了纯粹的限流器"
        )

    # ---- 4. 降级必须真的更快（§13.2）-----------------------------------
    if config.degrade_enabled and config.degrade_service_factor >= 1.0:
        report.problems.append(
            f"degrade_service_factor={config.degrade_service_factor} ≥ 1 —— "
            "降级后服务时间没有变短，白担了「结果变差」的代价"
        )

    # ---- 5. 丢弃阈值与超时预算必须自洽（§7.2 / §13.1）------------------
    feasible_wait_ms = config.request_budget_ms - config.service_time_estimate_ms
    critical_wait_ms = config.shed_critical_pressure * config.service_time_estimate_ms
    if config.shedding_enabled and critical_wait_ms <= feasible_wait_ms:
        report.problems.append(
            f"shed_critical_pressure={config.shed_critical_pressure} 对应的排队时间 "
            f"{critical_wait_ms:.0f}ms 不大于可行性边界 {feasible_wait_ms:.0f}ms —— "
            "DEADLINE_INFEASIBLE 会永远被 CRITICAL 丢弃抢先拦下，"
            "调用方只能拿到笼统的「系统忙」（§7.4 要求拒绝原因可行动）"
        )

    # ---- 6. 边缘限流要「略高于」总容量（§6.2）--------------------------
    if upstream is not None:
        capacity = upstream.capacity_rps
        if config.edge_rps <= capacity:
            report.problems.append(
                f"edge_rps={config.edge_rps} 不高于集群容量 {capacity:.2f} RPS —— "
                "L1 会先于准入控制拒掉正常流量，把误伤提前了"
            )

    # ---- 以下都是「对照组合法、生产危险」的配置 → notice ----------------
    if config.retry_budget_ratio is None:
        report.notices.append(
            "重试无预算（retry_budget_ratio=None）—— 朴素重试会在故障时放大负载 "
            "（§12.1）。仅用于对照组，生产禁用"
        )
    if config.cache_ttl_jitter <= 0 and (config.l1_capacity or config.l2_capacity):
        report.notices.append(
            "TTL 无抖动 —— 固定 TTL 会让缓存雪崩变成周期性事件（§10.2）"
        )
    if not config.sweep_expired:
        report.notices.append(
            "未开启超期清扫 —— 已超过 deadline 的请求会继续占着队列（§11.2）"
        )
    if not config.admission_enabled:
        report.notices.append(
            "未开启准入控制 —— 队列长度与延迟都不受控（§7）。仅用于对照组"
        )
    if (
        config.queue_policy is QueuePolicy.PRIORITY_AGING
        and config.aging_per_second <= 0
    ):
        report.notices.append(
            "使用优先级队列但老化权重为 0 —— 低优先级会被持续饿死（§8.2）"
        )
    if (
        config.autoscale_enabled
        and UpgradeSignal(config.autoscale_signal) is UpgradeSignal.CPU
    ):
        report.notices.append(
            "按 CPU 扩容 —— IO 密集型服务的 CPU 与负载几乎无关，"
            "这个 HPA 永远不会触发（§14.1）"
        )
    if config.semantic_cache_enabled and config.l1_capacity == 0 and config.l2_capacity == 0:
        report.notices.append("语义缓存已开但精确缓存容量为 0 —— 语义层没有可回填的条目")

    return report


# ======================================================================
# 目录里放一份可运行的热配置
# ======================================================================
def config_to_yaml(config: GatewayConfig) -> str:
    payload = {key: getattr(config, key) for key in GatewayConfig.model_fields}
    for key, value in list(payload.items()):
        if hasattr(value, "value"):
            payload[key] = value.value
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=True)
