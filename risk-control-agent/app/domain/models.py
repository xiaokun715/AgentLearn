"""风控 Agent 的领域模型。

对齐《第十八章：风控 Agent 设计说明书》：
- 一、§1.2 在线链路的数据结构     -> :class:`Event` / :class:`FeatureSet` /
                                      :class:`RuleHit` / :class:`ModelScore` /
                                      :class:`OnlineDecision`
- 三、§3.2 工具调用与网关裁决      -> :class:`ToolSpec` / :class:`ToolCall` /
                                      :class:`ToolVerdict`
- 四、§4.1 风险告警                -> :class:`Alert`
- 四、§4.3 证据                    -> :class:`Evidence`
- 四、§4.4 假设                    -> :class:`Hypothesis`
- 四、§4.6 调查报告                -> :class:`InvestigationReport`
- 五、§5   未知风险假设            -> :class:`RiskPattern`
- 六、§6   策略候选                -> :class:`StrategyCandidate`
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .enums import (
    Decision,
    Disposition,
    EvidenceKind,
    HypothesisStatus,
    InvestigationOutcome,
    Observation,
    PatternStatus,
    RiskLevel,
    ToolDenyReason,
    ToolKind,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


# ======================================================================
# 一、§1.2 在线决策链路
# ======================================================================
class Event(BaseModel):
    """一笔业务事件（注册 / 领券 / 下单 / 退款）。

    在线链路的输入是**事件流**，Agent 链路的输入是**事件聚合出来的告警**。
    这个粒度差异是两条链路的天然分界：在线链路处理的是「这一笔要不要放行」，
    Agent 处理的是「这批行为意味着什么」。
    """

    event_id: str = Field(default_factory=lambda: new_id("ev"))
    event_type: str                 # register / claim_coupon / place_order / refund
    user_id: str
    device_id: str = ""
    ip: str = ""
    merchant_id: str = ""
    amount: float = 0.0
    coupon_id: str = ""
    campaign_id: str = ""
    occurred_at_ms: int = 0
    #: 业务上下文（活动类型、商品类目……）—— 规则可以用它做场景区分
    context: dict[str, Any] = Field(default_factory=dict)


class FeatureSet(BaseModel):
    """特征计算结果。

    真机上这是一张很宽的表；Demo 只保留与规则/模型相关的那些，
    并且**每个特征都带一个 `source`** ——「这个数从哪来的」在风控里必须可回答，
    否则一个错误的特征会静默地影响所有决策，而没人知道该去修哪里。
    """

    values: dict[str, float] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict)
    computed_ms: int = 0

    def get(self, key: str, default: float = 0.0) -> float:
        return float(self.values.get(key, default))


class RuleHit(BaseModel):
    rule_id: str
    description: str
    risk_level: RiskLevel = RiskLevel.MEDIUM
    evidence: dict[str, Any] = Field(default_factory=dict)


class ModelScore(BaseModel):
    """风险模型的输出。**它只给分数，不给理由** —— 这是它的固有局限，
    也是「为什么还需要 Agent」的一部分原因（§2.1）。"""

    score: float = 0.0
    model_version: str = "risk-gbdt-v3"
    top_features: list[tuple[str, float]] = Field(default_factory=list)


class OnlineDecision(BaseModel):
    """在线决策链路的完整输出，含全部过程数据。

    **可解释是硬要求**：一笔被拦截的交易，风控人员必须能立刻回答
    「是规则拦的、模型拦的、还是两者都有」。
    """

    event_id: str
    decision: Decision
    rule_hits: list[RuleHit] = Field(default_factory=list)
    model_score: ModelScore = Field(default_factory=ModelScore)
    features: FeatureSet = Field(default_factory=FeatureSet)
    latency_ms: int = 0
    rationale: str = ""

    @property
    def hit_rule_ids(self) -> list[str]:
        return [hit.rule_id for hit in self.rule_hits]


# ======================================================================
# 四、§4.1 风险告警
# ======================================================================
class Alert(BaseModel):
    """在线链路升级出来的告警 —— Agent 链路的起点。"""

    alert_id: str = Field(default_factory=lambda: new_id("alert"))
    rule_ids: list[str] = Field(default_factory=list)
    entity_type: str = "merchant"       # merchant / user / device
    entity_id: str = ""
    reason: str = ""
    model_score: float = 0.0
    created_at_ms: int = 0
    #: 告警里带来的一点上下文（不足以判断，正是需要调查的原因）
    context: dict[str, Any] = Field(default_factory=dict)


# ======================================================================
# 四、§4.3 证据
# ======================================================================
class Evidence(BaseModel):
    """一条证据。

    ``derived_from`` 对推断类证据强制非空（§4.3）：
    **不能让「模型觉得可疑」直接变成一条事实**。
    """

    evidence_id: str = Field(default_factory=lambda: new_id("EV"))
    kind: EvidenceKind
    source: str                      # 产出它的工具
    claim: str
    payload: dict[str, Any] = Field(default_factory=dict)
    derived_from: list[str] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check_provenance(self) -> Evidence:
        if self.kind in (EvidenceKind.INFERRED, EvidenceKind.VERIFIED) and not self.derived_from:
            raise ValueError(f"{self.kind.value} 证据必须指回它的原始证据：{self.claim!r}")
        return self

    @property
    def is_raw(self) -> bool:
        return self.kind in (EvidenceKind.RAW, EvidenceKind.OBSERVED)


# ======================================================================
# 四、§4.4 假设
# ======================================================================
class Hypothesis(BaseModel):
    """一个风险假设。"""

    key: str
    description: str
    status: HypothesisStatus = HypothesisStatus.PENDING
    support: list[str] = Field(default_factory=list)      # 支持它的 evidence_id
    refute: list[str] = Field(default_factory=list)       # 反驳它的 evidence_id
    #: 由哪些工具检查过 —— 用于判断「是没查过，还是查过且排除了」
    checked_by: list[str] = Field(default_factory=list)
    weight: float = 0.0

    @property
    def active(self) -> bool:
        return self.status in (HypothesisStatus.PENDING, HypothesisStatus.UNDECIDED)


# ======================================================================
# 三、§3.2 工具与网关
# ======================================================================
class ToolSpec(BaseModel):
    """一个调查工具的声明。

    ``targets`` / ``preconditions`` 是**动态调查的基础**（§4.4）：
    规划器靠它判断「哪个工具能区分当前还活着的假设」，
    而不是靠工具名字猜。
    """

    name: str
    description: str
    kind: ToolKind = ToolKind.READ
    risk: RiskLevel = RiskLevel.LOW
    permission: str = "risk:read"
    #: **这个工具能区分哪些假设，以及异常观测分别意味着支持还是反驳。**
    #:
    #:     {"H_SHARED_DEVICE": "+", "H_LEGITIMATE": "-"}
    #:
    #: 值是 ``"+"``/``"-"``：``"+"`` 表示「该工具报 ANOMALY 时，这个假设被支持」，
    #: ``"-"`` 表示「被反驳」。``NORMAL`` 取反。
    #:
    #: **极性不能省。** 早期版本只有一个 ``targets`` 列表，语义被简化成了
    #: 「ANOMALY 支持列表里的所有假设」—— 于是设备图查出 7 个账号共用设备时，
    #: 它同时「支持」了 H_SHARED_DEVICE 和 H_LEGITIMATE（正常用户）。
    #: 一个工具能区分假设，不等于它支持全部假设。
    evidence_map: dict[str, str] = Field(default_factory=dict)

    @property
    def targets(self) -> list[str]:
        return list(self.evidence_map)
    #: 前置条件：需要已经查过哪些工具
    preconditions: list[str] = Field(default_factory=list)
    cost: float = 1.0
    latency_ms: int = 200
    #: 会不会改变世界 —— 写类工具必须有这个标记
    side_effect: bool = False
    #: 必填参数。缺参数的调用应该在**网关**被拦下，而不是在工具实现里抛异常 ——
    #: 前者是「这个请求不合法」，后者会被记成「工具执行失败」，两者的处置不同。
    required_args: list[str] = Field(default_factory=list)
    #: 哪个参数代表被调查的实体。网关会校验它在本次调查的**范围内**（§3.2）——
    #: 调查不能退化成对全库的任意查询。
    entity_arg: str | None = None


class ToolCall(BaseModel):
    """一次工具调用请求。"""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: 高危工具必须携带审批号（§3.2）
    approval_id: str | None = None
    reason: str = ""


class ToolVerdict(BaseModel):
    """工具网关的裁决。"""

    allowed: bool
    tool: str
    deny_reason: ToolDenyReason | None = None
    message: str = ""
    #: 是否被记录进审计（所有调用都会）
    audit_id: str = ""


class ToolResult(BaseModel):
    """一次工具调用的结果。"""

    tool: str
    observation: Observation = Observation.INCONCLUSIVE
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    latency_ms: int = 0
    #: 被网关拒绝时不为空
    denied: ToolDenyReason | None = None


# ======================================================================
# 四、§4.6 调查报告
# ======================================================================
class InvestigationReport(BaseModel):
    """Agent 的调查产物。

    ``disposition`` 是**建议**，不是执行结果。``executed`` 永远是 ``False`` ——
    这条边界由工具网关强制（§3.2），不是靠提示词约束。
    """

    alert_id: str
    entity_id: str
    outcome: InvestigationOutcome
    disposition: Disposition = Disposition.ESCALATE
    executed: bool = False                  # 永远是 False（Agent 无权处置）
    summary: str = ""
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    investigation_path: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    tool_calls: int = 0
    elapsed_ms: int = 0
    denied_calls: list[str] = Field(default_factory=list)
    ticket_id: str | None = None
    notes: list[str] = Field(default_factory=list)

    def to_report(self) -> str:
        lines = [
            f"告警      : {self.alert_id}",
            f"实体      : {self.entity_id}",
            f"结论      : {self.outcome.value}（置信度 {self.confidence:.2f}）",
            f"处置建议  : {self.disposition.value}（未执行）",
            f"证据条数  : {len(self.evidence_ids)}",
            f"调查路径  : {' → '.join(self.investigation_path)}",
        ]
        if self.missing_evidence:
            lines.append(f"缺失证据  : {self.missing_evidence}")
        return "\n".join(lines)


# ======================================================================
# 五、§5 未知风险假设
# ======================================================================
class RiskPattern(BaseModel):
    """一个候选风险模式（方式二：未知风险发现）。

    ``status`` 的迁移是本节的纪律：Agent 只能推到 ``CANDIDATE``；
    ``VALIDATED`` 必须由离线回放 + 人工确认给出。
    """

    pattern_id: str = Field(default_factory=lambda: new_id("pat"))
    name: str
    description: str
    status: PatternStatus = PatternStatus.HYPOTHESIS
    #: 构成这个模式的实体与关系
    members: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    #: 支持它的证据
    evidence_ids: list[str] = Field(default_factory=list)
    #: 反向证据：有没有「看起来像但不成立」的反例
    counter_evidence: list[str] = Field(default_factory=list)
    #: 离线回放的结果
    recall: float = 0.0
    precision: float = 0.0
    #: 建议转成什么（规则 / 特征 / 样本）
    proposed_as: str = ""
    decided_by: str = "agent"
    notes: list[str] = Field(default_factory=list)

    @property
    def promotable(self) -> bool:
        """能不能被提升为生产规则 —— 只有验证过的才可以。"""
        return self.status is PatternStatus.VALIDATED


# ======================================================================
# 六、§6 策略候选
# ======================================================================
class StrategyCandidate(BaseModel):
    """一条候选策略调整（方式三：规则与策略运营）。

    **Agent 生成的永远是候选，不是变更。** 发布需要走受控的策略发布系统，
    并且要有权限的人签字 —— 这条约束的理由在 §6.3：
    一扇被误关的规则留下的攻击窗口，代价远大于一次误报。
    """

    candidate_id: str = Field(default_factory=lambda: new_id("sc"))
    target_rule: str
    change_type: str = "adjust_threshold"   # adjust_threshold / add_feature / narrow_scope
    proposal: str = ""
    rationale: str = ""
    #: 离线评估的预期效果
    expected_fp_reduction: float = 0.0
    expected_fn_delta: float = 0.0
    risk_of_change: RiskLevel = RiskLevel.MEDIUM
    applied: bool = False                   # 永远 False（Agent 无权发布）
    requires_approval: bool = True
    evidence_ids: list[str] = Field(default_factory=list)
