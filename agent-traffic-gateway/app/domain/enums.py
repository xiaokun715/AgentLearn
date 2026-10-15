"""流量治理枚举 —— 拒绝原因、结果、优先级、降级级别、队列策略。

对齐《第十六章：高并发大流量 Agent 网关设计说明书》：
- §5   请求生命周期              -> ``app/gateway/gateway.py`` 的 11 阶段管线
- §6   多级限流                  -> :class:`RejectReason`
- §7.4 拒绝要给出可行动的信息     -> :class:`RejectReason` 的 ``actionable`` 属性
- §8.2 优先级                    -> :class:`Priority`
- §8.4 公平调度                  -> :class:`QueuePolicy`
- §10  分层缓存                  -> :class:`ServedBy`
- §12.4 熔断状态机               -> :class:`CircuitState`
- §13  分级降级                  -> :class:`ShedLevel`
"""
from __future__ import annotations

from enum import Enum


class Priority(int, Enum):
    """§8.2 优先级来自业务，不来自技术。

    数值越大越优先。``BATCH`` 的存在是刻意的：**批处理是过载时第一个该被牺牲的**，
    因为它对延迟最不敏感、价值密度最低。没有这一档，过载时系统只能
    「一视同仁地变慢」，而那是所有选项里最差的一个。
    """

    BATCH = 0          # 离线批处理、评测回放
    FREE = 1           # 免费额度 / 试用
    STANDARD = 2       # 普通付费
    INTERACTIVE = 3    # 交互式（用户正在等）
    CRITICAL = 4       # 大客户 SLA / 内部关键路径


class Outcome(str, Enum):
    """一次请求的最终结局。

    ``DEGRADED`` 单独成类而不是混进 ``OK``：降级是**隐性故障**，
    如果它不被单独计数，监控上就完全看不见（§16.1）。
    """

    OK = "OK"
    DEGRADED = "DEGRADED"     # 完成了，但质量下降
    REJECTED = "REJECTED"     # 被我方拒绝（限流 / 准入 / 队满 / 熔断）
    TIMEOUT = "TIMEOUT"       # 超过 deadline 仍未完成
    FAILED = "FAILED"         # 上游失败且重试耗尽


class RejectReason(str, Enum):
    """§6/§7/§12/§13 的拒绝原因 —— **必须分开统计**（§16.1）。

    一个 8% 的总拒绝率，如果全是 ``DEADLINE_INFEASIBLE``，说明容量不足；
    如果全是 ``TENANT_QUOTA_EXCEEDED``，说明是业务行为、系统很健康。
    混在一起看，什么都判断不出来。
    """

    TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"          # 全局边缘限流（§6.2 L1）
    TENANT_QUOTA_EXCEEDED = "TENANT_QUOTA_EXCEEDED"  # 租户配额（§6.2 L2）
    CIRCUIT_OPEN = "CIRCUIT_OPEN"                    # 熔断中（§12.4）
    DEADLINE_INFEASIBLE = "DEADLINE_INFEASIBLE"      # 准入：预计来不及（§7.2）
    QUEUE_FULL = "QUEUE_FULL"                        # 有界队列已满（§8.1）
    OVERLOADED = "OVERLOADED"                        # 降级到只收缓存命中（§13.1）
    UPSTREAM_ERROR = "UPSTREAM_ERROR"                # 上游错误且不重试

    @property
    def actionable(self) -> str:
        """§7.4 这个原因对调用方意味着什么 —— 决定它该不该重试。"""
        return {
            RejectReason.TOO_MANY_REQUESTS: "等一会儿重试（全局洪峰）",
            RejectReason.TENANT_QUOTA_EXCEEDED: "改配额，重试无用",
            RejectReason.CIRCUIT_OPEN: "等熔断恢复，立刻重试无用",
            RejectReason.DEADLINE_INFEASIBLE: "降级请求规模或放宽 deadline",
            RejectReason.QUEUE_FULL: "等一会儿重试或提高优先级",
            RejectReason.OVERLOADED: "等一会儿重试（系统正在降级）",
            RejectReason.UPSTREAM_ERROR: "可重试（但请遵守重试预算）",
        }[self]

    @property
    def is_quota(self) -> bool:
        """配额类拒绝（调用方改不了）vs 此刻类拒绝（等一会儿就好）。"""
        return self in (
            RejectReason.TENANT_QUOTA_EXCEEDED,
            RejectReason.TOO_MANY_REQUESTS,
        )


class ServedBy(str, Enum):
    """§10 请求最终由谁满足 —— 这是「减负效果」的度量单位。"""

    UPSTREAM = "upstream"
    CACHE_L1 = "cache_l1"
    CACHE_L2 = "cache_l2"
    CACHE_SEMANTIC = "cache_semantic"
    COALESCED = "coalesced"     # 搭了别人正在算的车（§9.1）


class QueuePolicy(str, Enum):
    """§8 调度策略。"""

    FIFO = "FIFO"                     # 先来先服务 —— 有公平性陷阱（§8.3）
    PRIORITY_AGING = "PRIORITY_AGING"  # 优先级 + 老化（§8.2）
    WFQ = "WFQ"                       # 加权公平队列 / 虚拟时间（§8.4）


class ShedLevel(str, Enum):
    """§13.1 按利用率决定丢弃边界。"""

    NORMAL = "NORMAL"        # 全收
    SOFT = "SOFT"            # 丢批处理
    HARD = "HARD"            # 只保留 INTERACTIVE 及以上
    CRITICAL = "CRITICAL"    # 只保留缓存命中

    @property
    def min_priority(self) -> Priority:
        """该级别下允许进入队列的最低优先级。"""
        return {
            ShedLevel.NORMAL: Priority.BATCH,
            ShedLevel.SOFT: Priority.FREE,
            ShedLevel.HARD: Priority.INTERACTIVE,
            ShedLevel.CRITICAL: Priority.CRITICAL,
        }[self]


class CircuitState(str, Enum):
    """§12.4 熔断状态机。"""

    CLOSED = "CLOSED"        # 正常放行
    OPEN = "OPEN"            # 快速失败
    HALF_OPEN = "HALF_OPEN"  # 探测放行


class UpgradeSignal(str, Enum):
    """§14.1 扩容指标 —— 选错指标等于永不扩容。"""

    CPU = "CPU"              # IO 密集型服务永远在 30%，从不触发
    QPS = "QPS"              # 忽略服务时间，上游变慢时完全失效
    QUEUE_DEPTH = "QUEUE_DEPTH"  # 直接反映服务能力是否跟得上
