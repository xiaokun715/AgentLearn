"""确定性策略层 —— 阈值、审批矩阵、假设库、预定义 Workflow 模板。

这一层的作用是**把「判断」从 Prompt 里搬到代码里**。说明书 §10.3 反复强调：

- ``enough_evidence_for_conclusion()`` 不应只依赖 LLM 的主观判断；
- ``validate_plan()`` 必须由确定性代码执行，不能仅依赖 Prompt 约束；
- ``execute_recovery()`` 必须检查权限、幂等性和副作用。

所以凡是能用数字和表格表达的判断，都写在这里。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .enums import FailureCategory, RiskTier


# ======================================================================
# §5.3 信息增益规划的权重
# ======================================================================
@dataclass(frozen=True)
class ScoringPolicy:
    """``Score(a) = IG(a) / (Cost(a) + λ·Risk(a) + ε)`` 的参数。

    ``λ`` 是风险权重。Demo 取 4.0 而不是 1.0，理由是**成本与风险不同量纲**：
    多花 2 秒重查一次日志，和多 0.2 的风险去动一次设备，在排障现场根本不是一回事。
    真机上这个数应该由历史数据回归出来，而不是拍脑袋 —— 这里给一个能说明问题的默认值。
    """

    risk_weight: float = 4.0
    epsilon: float = 0.25
    #: 低于此 IG 的动作不值得花一次工具调用（例如「重复确认已知事实」的检查）
    min_information_gain: float = 0.02


SCORING = ScoringPolicy()


# ======================================================================
# §9 结论门（确定性判据，替代「让 LLM 说够不够」）
# ======================================================================
@dataclass(frozen=True)
class ConclusionPolicy:
    """什么叫做「有充分证据」—— 三条硬门槛，缺一不可。"""

    #: 领跑假设的后验概率下限
    accept_probability: float = 0.75
    #: 低于此值的假设直接标 REFUTED（被证据排除）
    refute_probability: float = 0.06
    #: 领跑假设至少需要的**独立证据来源数**。
    #:
    #: 注意口径是「来源数」而不是「条数」：一次检查会同时产出原始证据与观测事实，
    #: 按条数算的话单次检查就凑够了 2 条，这道门就形同虚设。
    #: 要求两个来源，等于要求「至少两个不同角度的检查互相印证」。
    min_independent_sources: int = 2
    #: 领跑假设至少要有一次**主动推动过它**的检查。
    #: 这一条专治「没被反驳 ≠ 被支持」—— 否则「什么都没查」也能凑出结论。
    require_discriminating_check: bool = True
    #: |Δposterior| 达到这个幅度才算「这次检查对本假设有区分力」
    posterior_move_epsilon: float = 0.08
    #: |Δposterior| 达到这个幅度才把证据挂到假设上。
    #: **刻意低于** ``posterior_move_epsilon``：一次只挪动 3 个点的检查依然是
    #: 弱支持（该记在账上），但它不配被称为「有区分力的检查」。
    evidence_attach_epsilon: float = 0.02
    #: 单次检查若把领跑假设推高这么多，视为**决定性检查**。
    #:
    #: 有了它，第二道门可以是「两个独立来源互相印证 **或** 一次决定性检查」——
    #: 这两种都是站得住的认知状态。若只认「两个来源」，一个一锤定音的检查
    #: 反而永远结不了案（因为它已经把概率顶满，后续检查挪不动它了）。
    decisive_move: float = 0.20


CONCLUSION = ConclusionPolicy()


# ======================================================================
# §7.1 恢复动作的审批矩阵
# ======================================================================
APPROVAL_MATRIX: dict[RiskTier, str] = {
    RiskTier.R0: "AUTO",       # 查询日志、读取配置
    RiskTier.R1: "AUTO",       # 无副作用的状态刷新、只读校验
    RiskTier.R2: "QUOTA",      # 重新申请环境、重跑可重复测试：查配额/成本/幂等
    RiskTier.R3: "APPROVE",    # 重启设备、修改配置：人工审批
    RiskTier.R4: "FORBIDDEN",  # 改测试预期、覆盖正式配置：默认禁止自动执行
}


# ======================================================================
# §4.1 → §5.1 分类到假设的映射
# ======================================================================
@dataclass(frozen=True)
class HypothesisSpec:
    """假设库里的一个条目 —— 描述、归属类别、一旦成立需要做的检查。"""

    key: str
    description: str
    category: FailureCategory
    required_checks: list[str] = field(default_factory=list)


HYPOTHESIS_LIBRARY: dict[str, HypothesisSpec] = {
    "H_CONFIG": HypothesisSpec(
        key="H_CONFIG",
        description="测试配置与需求约束不一致（参数缺失或取值不合法）",
        category=FailureCategory.CONFIG,
        required_checks=["inspect_config"],
    ),
    "H_ENV": HypothesisSpec(
        key="H_ENV",
        description="测试环境没有正确初始化（设备不可用 / 资源未绑定）",
        category=FailureCategory.ENVIRONMENT,
        required_checks=["check_environment_state"],
    ),
    "H_SOFTWARE": HypothesisSpec(
        key="H_SOFTWARE",
        description="被测软件存在功能异常，行为与协议要求不一致",
        category=FailureCategory.SOFTWARE,
        required_checks=["analyze_logs", "retrieve_raw_log"],
    ),
    "H_CASE": HypothesisSpec(
        key="H_CASE",
        description="测试用例的预期结果定义错误（与需求约束不符）",
        category=FailureCategory.TEST_CASE,
        required_checks=["validate_case_definition"],
    ),
    "H_TOOL": HypothesisSpec(
        key="H_TOOL",
        description="测试工具没有正确采集实际结果（采集中断 / 返回码异常）",
        category=FailureCategory.TOOL_EXECUTION,
        required_checks=["inspect_tool_records"],
    ),
}

#: 分类类别 → 该类别下优先考虑的假设及其先验概率。
#: 先验是**分类器给出的起点**，不是结论；后续由贝叶斯更新接管。
CATEGORY_HYPOTHESIS_PRIORS: dict[FailureCategory, dict[str, float]] = {
    FailureCategory.ENVIRONMENT: {"H_ENV": 0.45, "H_TOOL": 0.20, "H_CONFIG": 0.15, "H_SOFTWARE": 0.15, "H_CASE": 0.05},
    FailureCategory.CONFIG: {"H_CONFIG": 0.55, "H_ENV": 0.15, "H_TOOL": 0.15, "H_SOFTWARE": 0.10, "H_CASE": 0.05},
    FailureCategory.TEST_CASE: {"H_CASE": 0.50, "H_CONFIG": 0.20, "H_SOFTWARE": 0.15, "H_ENV": 0.10, "H_TOOL": 0.05},
    FailureCategory.SOFTWARE: {"H_SOFTWARE": 0.50, "H_ENV": 0.20, "H_TOOL": 0.15, "H_CONFIG": 0.10, "H_CASE": 0.05},
    FailureCategory.TOOL_EXECUTION: {"H_TOOL": 0.50, "H_ENV": 0.25, "H_SOFTWARE": 0.15, "H_CONFIG": 0.05, "H_CASE": 0.05},
    FailureCategory.UNKNOWN: {"H_ENV": 0.22, "H_CONFIG": 0.20, "H_TOOL": 0.20, "H_SOFTWARE": 0.20, "H_CASE": 0.18},
}


# ======================================================================
# §6.2 模式 A：预定义 Workflow 模板
# ======================================================================
@dataclass(frozen=True)
class WorkflowTemplate:
    """常见故障的固定诊断流程。

    说明书要求「优先执行已知模板；只有模板不适用时才动态生成」。
    模板的价值在**稳定、容易测试、成本可预期**；代价是它会做完所有检查，
    哪怕第一个检查就已经足够定性 —— 这正是模式 C 要赢回来的那部分开销。
    """

    template_id: str
    description: str
    match_categories: list[FailureCategory]
    #: (节点 ID, skill, 依赖节点 ID 列表, 该节点可复用的缓存键)
    nodes: list[tuple[str, str, list[str], str | None]]


WORKFLOW_TEMPLATES: list[WorkflowTemplate] = [
    WorkflowTemplate(
        template_id="TPL_ENV_FIRST",
        description="环境类故障：环境体检 → 日志取证 → 工具记录 → 配置比对",
        match_categories=[
            FailureCategory.ENVIRONMENT,
            FailureCategory.TOOL_EXECUTION,
            FailureCategory.UNKNOWN,
        ],
        nodes=[
            ("N1", "check_environment_state", [], "environment_state"),
            ("N2", "analyze_logs", [], None),
            ("N3", "inspect_tool_records", ["N2"], None),
            ("N4", "inspect_config", [], "test_config"),
        ],
    ),
    WorkflowTemplate(
        template_id="TPL_CONFIG_FIRST",
        description="配置类故障：配置差异 → 用例定义 → 环境体检",
        match_categories=[FailureCategory.CONFIG, FailureCategory.TEST_CASE],
        nodes=[
            ("N1", "inspect_config", [], "test_config"),
            ("N2", "validate_case_definition", [], None),
            ("N3", "check_environment_state", ["N1"], "environment_state"),
        ],
    ),
]


def template_for(category: FailureCategory) -> WorkflowTemplate | None:
    """按分类挑选模板（模板不适用时返回 ``None`` 交给模式 C）。"""
    for template in WORKFLOW_TEMPLATES:
        if category in template.match_categories:
            return template
    return None


# ======================================================================
# 演示用的权限集（真机上来自统一的鉴权中心）
# ======================================================================
DEFAULT_PERMISSIONS: set[str] = {
    "diagnose:read",        # 读取日志 / 配置 / 环境状态
    "recovery:refresh",     # R1 只读刷新
    "recovery:reallocate",  # R2 重新申请环境 / 重跑测试
    # 注意：这里**刻意没有** "recovery:restart-device"（R3，需审批）
    #      和 "recovery:override-expectation"（R4，禁止）。见 README「关键取舍 4」。
}

#: 经过人工审批之后临时授予的权限（§7.2 授权策略）
APPROVAL_GRANTS: dict[str, str] = {
    "restart_device": "recovery:restart-device",
}
