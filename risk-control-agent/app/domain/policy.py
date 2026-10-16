"""规则、假设、工具权限矩阵与阈值 —— 全章的「判断」都集中在这里。

对齐说明书：
- 一、§1.3 在线规则与决策阈值    -> :data:`ONLINE_RULES` / :class:`DecisionPolicy`
- 三、§3.2 工具权限矩阵           -> :data:`TOOL_SPECS` / :data:`AGENT_GRANTS`
- 四、§4.4 风险假设库             -> :data:`HYPOTHESIS_LIBRARY`
- 四、§4.4 调查预算               -> :class:`InvestigationBudget`
- 六、§6.3 策略变更的审批边界     -> :data:`STRATEGY_POLICY`
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .enums import RiskLevel, ToolKind
from .models import ToolSpec


# ======================================================================
# 一、§1.3 在线规则
# ======================================================================
@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    description: str
    feature: str
    threshold: float
    #: 比较方向。**不能省。** 早期版本对所有规则都用 ``value > threshold``，
    #: 于是「注册后 10 分钟内领券」这条规则变成了「注册超过 10 分钟」——
    #: 它对每一个老用户都命中。这类 bug 不会报错，只会让规则**反着生效**，
    #: 而且从告警量上看起来「规则很灵敏」。
    direction: str = "above"        # above: value > threshold；below: value < threshold
    risk_level: RiskLevel = RiskLevel.MEDIUM
    #: 只在特定场景生效（如促销活动）——「活动的交易特征和平时不一样」
    scene: str | None = None
    #: 该规则上线以来的人工审核误报率（用于离线分析场景）
    observed_fp_rate: float = 0.0


ONLINE_RULES: list[RuleSpec] = [
    RuleSpec(
        rule_id="R001_COUPON_VELOCITY",
        description="单账号单日领券次数超过阈值",
        feature="coupon_claims_1d",
        threshold=5,
        risk_level=RiskLevel.MEDIUM,
    ),
    RuleSpec(
        rule_id="R002_DEVICE_SHARING",
        description="同一设备 24 小时内关联的账号数超过阈值",
        feature="accounts_per_device_1d",
        threshold=3,
        risk_level=RiskLevel.HIGH,
    ),
    RuleSpec(
        rule_id="R003_REFUND_RATIO",
        description="7 日退款率超过阈值且退款笔数达到门槛",
        feature="refund_ratio_7d",
        threshold=0.55,
        risk_level=RiskLevel.HIGH,
        observed_fp_rate=0.31,   # **这条规则的误报率偏高** —— 策略场景的主角
    ),
    RuleSpec(
        rule_id="R004_NEW_ACCOUNT_CLAIM",
        description="注册后极短时间内即领券",
        feature="minutes_since_register",
        threshold=10.0,
        risk_level=RiskLevel.MEDIUM,
        direction="below",
    ),
    RuleSpec(
        rule_id="R005_IP_CLUSTER",
        description="同一 IP 一小时内出现的账号数超过阈值",
        feature="accounts_per_ip_1h",
        threshold=10,
        risk_level=RiskLevel.MEDIUM,
    ),
]


# ======================================================================
# 一、§1.3 决策阈值
# ======================================================================
@dataclass(frozen=True)
class DecisionPolicy:
    """规则与模型分数如何合成一个决策。

    **阈值必须写在一处、并且可以被离线回放口径对齐** ——
    散落在代码里的魔法数字，是策略无法被评估的根本原因。
    """

    block_score: float = 0.85
    verify_score: float = 0.60
    review_score: float = 0.45
    #: 命中两条及以上规则即转人工 ——「单条都够不上，合起来够得上」
    review_on_multi_hit: int = 2
    #: 促销活动期间的退款率阈值上浮（活动天然带来高退款）
    promo_refund_relax: float = 0.25


DECISION = DecisionPolicy()


# ======================================================================
# 四、§4.4 风险假设库
# ======================================================================
@dataclass(frozen=True)
class HypothesisSpec:
    key: str
    description: str
    #: 成立时通常意味着什么处置
    severity: RiskLevel = RiskLevel.MEDIUM


HYPOTHESIS_LIBRARY: dict[str, HypothesisSpec] = {
    "H_BULK_REGISTER": HypothesisSpec(
        "H_BULK_REGISTER", "存在批量注册的真实用户/虚假用户", RiskLevel.HIGH),
    "H_SHARED_DEVICE": HypothesisSpec(
        "H_SHARED_DEVICE", "多个账号共享设备（设备池 / 模拟器）", RiskLevel.HIGH),
    "H_REFUND_ABUSE": HypothesisSpec(
        "H_REFUND_ABUSE", "利用退款流程套利", RiskLevel.HIGH),
    "H_LEGITIMATE": HypothesisSpec(
        "H_LEGITIMATE", "正常用户行为，告警属于误报", RiskLevel.LOW),
    "H_INSUFFICIENT_DATA": HypothesisSpec(
        "H_INSUFFICIENT_DATA", "关键数据缺失或不可用，无法判断", RiskLevel.MEDIUM),
}


# ======================================================================
# 三、§3.2 工具清单与权限矩阵
# ======================================================================
TOOL_SPECS: list[ToolSpec] = [
    # ---------------- 只读查询 ----------------
    ToolSpec(
        name="query_account_profile",
        required_args=["user_id"], entity_arg="user_id",
        description="查询账号画像：注册时间、等级、历史订单与领券次数",
        kind=ToolKind.READ, permission="risk:read", cost=1.0, latency_ms=180,
        evidence_map={"H_BULK_REGISTER": "+", "H_LEGITIMATE": "-"},
    ),
    ToolSpec(
        name="query_device_graph",
        required_args=["device_id"], entity_arg="device_id",
        description="查询设备关联：该设备近 24h 关联的账号列表",
        kind=ToolKind.READ, permission="risk:read", cost=1.0, latency_ms=260,
        evidence_map={"H_SHARED_DEVICE": "+", "H_BULK_REGISTER": "+", "H_LEGITIMATE": "-"},
    ),
    ToolSpec(
        name="query_transaction_history",
        required_args=["user_id"], entity_arg="user_id",
        description="查询交易与退款明细",
        kind=ToolKind.READ, permission="risk:read", cost=1.5, latency_ms=320,
        evidence_map={"H_REFUND_ABUSE": "+", "H_LEGITIMATE": "-"},
    ),
    ToolSpec(
        name="query_coupon_usage",
        required_args=["user_id"], entity_arg="user_id",
        description="查询券领取与核销记录",
        kind=ToolKind.READ, permission="risk:read", cost=1.0, latency_ms=210,
        evidence_map={"H_REFUND_ABUSE": "+", "H_BULK_REGISTER": "+", "H_LEGITIMATE": "-"},
    ),
    ToolSpec(
        name="query_ip_cluster",
        required_args=["ip"], entity_arg="ip",
        description="查询同 IP 聚集情况",
        kind=ToolKind.READ, permission="risk:read", cost=1.0, latency_ms=240,
        # IP 聚集是**网络**维度：NAT、公司出口、公共 WiFi 都会造成聚集，
        # 它不能用来判断「是否共享设备」。给工具声明它答不了的假设，
        # 会让规划器以为这一维已经被检验过了（§4.4）。
        evidence_map={"H_BULK_REGISTER": "+", "H_LEGITIMATE": "-"},
    ),
    ToolSpec(
        name="query_risk_model_detail",
        description="查询风险模型的分数与主要贡献特征",
        kind=ToolKind.READ, permission="risk:read", cost=0.5, latency_ms=90,
        evidence_map={},   # 它不区分假设，只解释「模型为什么这么打分」
    ),
    # ---------------- 计算类 ----------------
    ToolSpec(
        name="run_graph_analysis",
        required_args=["device_id"], entity_arg="device_id",
        description="对账号-设备-IP 关系图做联通分量与团伙检测",
        kind=ToolKind.COMPUTE, permission="risk:graph", risk=RiskLevel.MEDIUM,
        cost=2.5, latency_ms=900,
        evidence_map={"H_SHARED_DEVICE": "+", "H_BULK_REGISTER": "+", "H_LEGITIMATE": "-"},
        preconditions=["query_device_graph"],
    ),
    # ---------------- 写类 ----------------
    ToolSpec(
        name="search_similar_cases",
        required_args=["keyword"],
        description="检索历史相似案件与已确认的攻击模式",
        kind=ToolKind.READ, permission="risk:read", cost=0.8, latency_ms=300,
        evidence_map={},   # 历史案例提供「先例」，不直接区分假设
    ),
    ToolSpec(
        name="create_case_ticket",
        required_args=["alert_id", "summary"],
        description="创建处置工单并关联证据（写业务对象）",
        kind=ToolKind.WRITE, permission="ops:write", risk=RiskLevel.MEDIUM,
        cost=0.5, latency_ms=150, side_effect=True,
    ),
    # ---------------- 高危：执行处置 ----------------
    ToolSpec(
        name="apply_disposition",
        required_args=["entity_id", "action"],
        description="执行处置动作（放行 / 保持拦截 / 加分 / 扣分）",
        kind=ToolKind.WRITE, permission="risk:enforce", risk=RiskLevel.HIGH,
        cost=0.5, latency_ms=120, side_effect=True,
    ),
    ToolSpec(
        name="release_block",
        required_args=["entity_id"], entity_arg="entity_id",
        description="解除对账号/设备的拦截",
        kind=ToolKind.WRITE, permission="risk:enforce", risk=RiskLevel.HIGH,
        cost=0.5, latency_ms=120, side_effect=True,
    ),
    # ---------------- 最高危：改风控配置 ----------------
    ToolSpec(
        name="update_rule_config",
        required_args=["rule_id", "threshold"],
        description="修改线上规则的阈值或适用范围",
        kind=ToolKind.ADMIN, permission="risk:admin", risk=RiskLevel.HIGH,
        cost=1.0, latency_ms=200, side_effect=True,
    ),
    ToolSpec(
        name="publish_strategy",
        required_args=["candidate_id"],
        description="发布策略变更到生产环境",
        kind=ToolKind.ADMIN, permission="risk:admin", risk=RiskLevel.HIGH,
        cost=1.0, latency_ms=200, side_effect=True,
    ),
]

TOOL_INDEX: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

#: Agent 的默认权限集。
#:
#: **这里没有 `risk:enforce`，也没有 `risk:admin`** —— 它不是「暂时忘了加」，
#: 而是本设计的核心约束（§3.2）：调查 Agent 永远不能执行处置、永远不能改规则。
#: 把这两件事交给同一条链路，等于让一个会犯错的推理过程直接拥有生产写权限。
AGENT_GRANTS: set[str] = {"risk:read", "risk:graph", "ops:write"}

#: 人工审批通过后临时授予的权限（§3.2 的例外通道）
#: 注意 ``risk:admin`` **不在这里** —— 改规则没有「Agent 申请一下就放行」的通道。
APPROVAL_GRANTS: dict[str, str] = {
    "apply_disposition": "risk:enforce",
    "release_block": "risk:enforce",
}


# ======================================================================
# 四、§4.4 调查预算
# ======================================================================
@dataclass(frozen=True)
class InvestigationBudget:
    """一次调查的硬约束。

    没有预算的 Agent 调查会退化成「把所有能查的都查一遍」——
    那既慢又贵，而且**结论并不会更好**：多查的证据里大部分与当前假设无关。
    """

    max_tool_calls: int = 10
    max_elapsed_ms: int = 30_000
    max_cost: float = 12.0
    #: 连续多少次「无新信息」就停止 —— 防止在两个假设之间来回晃
    max_barren_rounds: int = 2


BUDGET = InvestigationBudget()


# ======================================================================
# 四、§4.5 结论门
# ======================================================================
@dataclass(frozen=True)
class ConclusionPolicy:
    """什么叫做「证据充分」。

    **必须由确定性代码判定，不能交给模型自评。** 与第十五章同源的纪律：
    当「我查够了」由模型说了算时，它会随上下文长度、提示词措辞、
    甚至当天的采样温度而变 —— 于是同一个案子跑两次可能给出不同的结论。
    """

    #: 支持结论的最少独立证据来源数。
    #:
    #: 取 3 而不是 2：两个来源很容易凑出来（一次检查同时更新两个假设），
    #: 而风控结论的代价是不对称的 —— 一个「确认风险」会导致封号，
    #: 一个「确认误报」会放过真实的黑产。多查一步的成本远低于判错的成本。
    min_supporting_sources: int = 3
    #: 需要有多少条反驳证据，才算「排除」了一个假设
    refute_threshold: int = 1
    #: 低于这个置信度不下结论
    min_confidence: float = 0.5


CONCLUSION = ConclusionPolicy()


# ======================================================================
# 六、§6.3 策略变更的审批边界
# ======================================================================
@dataclass
class StrategyPolicy:
    """Agent 能做什么、不能做什么。

    这张表是本 Demo 存在的主要理由之一。**它的每一条都对应一个真实事故模式**：

    * 让 Agent 直接关规则 → 攻击窗口，且没人知道是谁关的；
    * 让 Agent 放宽阈值来降误报 → 指标好看了，风险敞口变大了；
    * 让 Agent 自己验证自己的假设 → 它总会找到支持自己的证据。
    """

    #: Agent 可以做的
    can: tuple[str, ...] = (
        "分析误报/漏报案例并归纳共性",
        "提出候选策略与验证方案",
        "生成离线回放任务并汇总指标",
        "创建工单、补充调查记录",
    )
    #: Agent 明确不能做的
    cannot: tuple[str, ...] = (
        "修改线上规则或阈值",
        "发布策略到生产",
        "解除拦截或改变处置结果",
        "把未验证的风险假设标记为已确认",
        "用「降低误报」作为放宽阈值的唯一理由",
    )
    #: 候选策略必须携带的字段，缺一项就不允许进入评审
    required_fields: tuple[str, ...] = (
        "rationale",
        "expected_fp_reduction",
        "expected_fn_delta",     # 漏报影响必须显式给出
        "risk_of_change",
        "evidence_ids",
    )
    min_approved_evidence: int = 2


STRATEGY_POLICY = StrategyPolicy()


@dataclass
class ScenePolicy:
    """场景相关的阈值调整（§6.2 ——「规则没有考虑活动场景」）。"""

    #: 促销活动中，退款率规则的阈值上浮系数
    promo_refund_factor: float = 1.4
    promo_campaign_types: tuple[str, ...] = field(
        default=("promo_double11", "promo_618", "promo_newyear"))


SCENE = ScenePolicy()
