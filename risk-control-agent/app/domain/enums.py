"""风控 Agent 的枚举。

对齐《第十八章：风控 Agent 设计说明书》：
- 一、§1.3 在线决策的四种处置    -> :class:`Decision`
- 二、§2   四条接入链路           -> :class:`AgentMission`
- 三、§3.2 工具风险等级与权限     -> :class:`ToolKind` / :class:`ToolDenyReason`
- 四、§4   调查的观测与假设       -> :class:`Observation` / :class:`HypothesisStatus`
- 四、§4.5 调查结论               -> :class:`InvestigationOutcome`
- 五、§5   未知风险假设的生命周期  -> :class:`PatternStatus`
"""
from __future__ import annotations

from enum import Enum


class Decision(str, Enum):
    """§1.3 在线决策引擎的四种输出。

    **在线链路的输出必须是确定的、可解释的、毫秒级的**。
    它不「调查」，只做匹配与打分 —— 这是它与 Agent 链路的根本分工。
    """

    PASS = "PASS"        # 放行
    VERIFY = "VERIFY"    # 二次验证（短信/人脸）
    BLOCK = "BLOCK"      # 拦截
    REVIEW = "REVIEW"    # 转人工审核

    @property
    def is_online_final(self) -> bool:
        """``REVIEW`` 不是终态 —— 它把决定权交出去（人，或者 Agent）。"""
        return self is not Decision.REVIEW


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def order(self) -> int:
        return {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[self.value]


class ToolKind(str, Enum):
    """§3.2 工具的三类，权限模型完全不同。

    ``READ`` / ``COMPUTE`` 是调查；``WRITE`` / ``ADMIN`` 是**改世界**。
    把它们混在一个权限维度里，是权限设计最常见的错误 ——
    「能查交易记录」和「能改风控规则」被授成同一个权限，
    那么任何一个调查 Agent 出问题都会变成生产事故。
    """

    READ = "READ"        # 只读查询
    COMPUTE = "COMPUTE"  # 关联分析、图计算（不改数据，但消耗资源）
    WRITE = "WRITE"      # 写业务对象：工单、备注、标签
    ADMIN = "ADMIN"      # 改风控配置：规则、阈值、策略


class ToolDenyReason(str, Enum):
    """§3.2 工具网关的拒绝原因 —— 每一种都要能被单独统计。"""

    NO_PERMISSION = "NO_PERMISSION"              # 缺权限
    NEEDS_APPROVAL = "NEEDS_APPROVAL"            # 高危：需要人工审批
    UNKNOWN_TOOL = "UNKNOWN_TOOL"                # 不在工具清单里
    BAD_ARGUMENTS = "BAD_ARGUMENTS"              # 参数校验失败
    OUT_OF_SCOPE = "OUT_OF_SCOPE"                # 实体不在本次任务范围内
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"        # 调用预算耗尽
    PRECONDITION_FAILED = "PRECONDITION_FAILED"  # 前置条件不满足


class Observation(str, Enum):
    """§4.2 一次调查工具的观测结果。

    与第十五章的排障系统同源：**``INCONCLUSIVE`` 与 ``NORMAL`` 必须分开**。
    把「查不到」当成「没问题」，是调查类系统最危险的自我欺骗 ——
    它会让人把「证据不足」当成「已经排除」。
    """

    ANOMALY = "ANOMALY"
    NORMAL = "NORMAL"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"


class HypothesisStatus(str, Enum):
    PENDING = "PENDING"
    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    UNDECIDED = "UNDECIDED"


class InvestigationOutcome(str, Enum):
    """§4.5 一次调查的结论。**它不是一个布尔值。**

    把结论压成「有风险 / 没风险」，会让「证据不足」这个最常见的真实状态
    无处安放 —— 于是它会被硬塞进两者之一，通常被塞进「没风险」。
    """

    CONFIRMED_RISK = "CONFIRMED_RISK"              # 证据充分，确认风险
    LIKELY_FALSE_POSITIVE = "LIKELY_FALSE_POSITIVE"  # 证据指向正常，建议放行
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # 查了但不够，转人工
    NEEDS_HUMAN_JUDGEMENT = "NEEDS_HUMAN_JUDGEMENT"  # 涉及业务判断，Agent 不该拍板


class AgentMission(str, Enum):
    """§2 四条接入链路 —— 一个 Agent 平台通常同时提供这四种任务。"""

    INVESTIGATE = "INVESTIGATE"          # 方式一：风险事件调查
    DISCOVER_PATTERN = "DISCOVER_PATTERN"  # 方式二：未知风险发现（离线）
    OPTIMIZE_STRATEGY = "OPTIMIZE_STRATEGY"  # 方式三：规则与策略运营
    OPS_WORKFLOW = "OPS_WORKFLOW"        # 方式四：安全运营流程


class PatternStatus(str, Enum):
    """§5 未知风险假设的生命周期。

    关键约束：Agent 只能把假设推进到 ``CANDIDATE``。
    从 ``CANDIDATE`` 到 ``VALIDATED`` 必须经过**离线回放 + 人工确认** ——
    这是「模型觉得可疑」与「数据证明是风险」之间的那道墙。
    """

    HYPOTHESIS = "HYPOTHESIS"    # Agent 提出的初始猜测
    CANDIDATE = "CANDIDATE"      # 有数据支撑，待验证
    VALIDATED = "VALIDATED"      # 离线回放 + 人工确认通过
    REJECTED = "REJECTED"        # 回放不通过
    #: 需要新的数据源/特征才能继续验证 —— 一个诚实的中间状态
    NEEDS_MORE_DATA = "NEEDS_MORE_DATA"


class Disposition(str, Enum):
    """§4.5 Agent 给出的**处置建议**。

    注意名字：是「建议」。Agent 输出建议，**执行**是另一条链路上的事 ——
    这条边界在本 Demo 里由工具网关强制执行（§3.2）。
    """

    KEEP_BLOCK = "KEEP_BLOCK"          # 维持拦截
    RELEASE = "RELEASE"                # 解除拦截
    ESCALATE = "ESCALATE"              # 升级为人工审核
    REQUEST_VERIFY = "REQUEST_VERIFY"  # 追加验证手段
    CLOSE_AS_FALSE_POSITIVE = "CLOSE_AS_FALSE_POSITIVE"


class EvidenceKind(str, Enum):
    """§4.3 证据分层 —— 与第十五章同一套纪律。"""

    RAW = "RAW"                # 数据源原文
    OBSERVED = "OBSERVED"      # 工具给出的确定性观测
    INFERRED = "INFERRED"      # 模型/规则的推断
    VERIFIED = "VERIFIED"      # 交叉验证或人工确认过的结论
