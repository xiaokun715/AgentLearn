"""排障领域模型 —— 全系统共享的数据契约。

对齐《第十五章：排障 Agent 设计》：
- 三、§3.1 统一的故障上下文        -> :class:`FailureContext`
- 三、§3.2 统一事件模型            -> :class:`TraceEvent`
- 三、§3.1 证据分层                -> :class:`Evidence`
- 五、§5.1 根因假设记录            -> :class:`Hypothesis`
- 五、§5.3 诊断动作与似然表        -> :class:`ActionSpec`
- 六、§6.3 结构化节点计划          -> :class:`PlanNode` / :class:`DiagnosticPlan`
- 六、§6.3 Workflow 校验结论       -> :class:`ValidationIssue` / :class:`ValidationReport`
- 七、§7.1 受控恢复决策            -> :class:`RecoveryDecision`
- 八、§8.1 故障经验数据结构        -> :class:`FailureSignature` / :class:`ExperienceCase`
- 九、§9   终止状态与核心指标      -> :class:`DiagnosisConclusion`
- 十、§10.3 循环预算约束           -> :class:`Budget` / :class:`BudgetUsage`
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .enums import (
    EvidenceKind,
    ExperienceStatus,
    FailureCategory,
    HypothesisStatus,
    NodeStatus,
    Observation,
    PlanMode,
    RiskTier,
    TerminalState,
)


# ======================================================================
# 基础工具
# ======================================================================
def utcnow() -> datetime:
    """带时区的当前时间。全系统统一走这里，便于测试注入。"""
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    """生成 ``EV_3f2a1b9c`` 形式的短 ID。"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def compact(payload: Any, limit: int = 240) -> str:
    """把任意结构压成一行短文本 —— 用于日志与终端展示，不用于存储。"""
    if isinstance(payload, str):
        flat = " ".join(payload.split())
    else:
        flat = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def stable_hash(payload: Any) -> str:
    """稳定哈希：键排序 + 非 ASCII 不转义。

    ``{"a":1,"b":2}`` 与 ``{"b":2,"a":1}`` 必须得到同一个值，
    否则同一个故障指纹会因为字典顺序不同而被当成两个案例。
    """
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# ======================================================================
# 三、§3.2 统一事件模型
# ======================================================================
class TraceEvent(BaseModel):
    """§3.2 执行轨迹的统一事件。

    **刻意不记录大模型内部的隐藏推理过程** —— 说明书原文：
    「对于大模型内部的隐藏推理过程，不需要也不应该将其作为可审计轨迹。
    应该记录的是模型的结构化结论、输入输出引用、所选择的动作及其依据。」
    所以这里的 ``metadata`` 存的是结构化结论，不是 chain-of-thought。

    ``input_ref`` / ``output_ref`` 用 ``artifact://`` 引用而不是内联正文：
    大段日志留在对象存储里，轨迹库只存元数据与引用。
    """

    trace_id: str
    task_id: str
    event_id: str
    parent_event_id: str | None = None
    agent: str = ""
    skill: str = ""
    event_type: str = "tool_call"       # agent_start/agent_end/skill_call/tool_call/model_call/state_change/retry/recovery/env_change
    tool: str | None = None
    status: str = "OK"                  # OK / FAILED / SKIPPED
    start_time: str = ""
    duration_ms: int = 0
    input_ref: str | None = None
    output_ref: str | None = None
    error_code: str | None = None
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status.upper() in ("FAILED", "ERROR")

    @property
    def claimed_success(self) -> bool:
        return self.status.upper() in ("OK", "SUCCESS", "SUCCEEDED")


# ======================================================================
# 三、§3.1 统一的故障上下文
# ======================================================================
class FailureSignature(BaseModel):
    """§8.3 用于故障检索的指纹。

    检索维度照着说明书列：故障类型、错误码与日志模式、软件版本、用例类型、
    环境配置、失败发生的执行阶段、已完成的检查。
    """

    error_codes: list[str] = Field(default_factory=list)
    log_patterns: list[str] = Field(default_factory=list)
    failure_category: str = FailureCategory.UNKNOWN.value
    software_version: str = ""
    test_case_kind: str = ""
    failed_stage: str = ""
    environment: dict[str, Any] = Field(default_factory=dict)

    def match_score(self, other: FailureSignature) -> float:
        """加权相似度，落在 ``[0, 1]``。用于经验检索后的**适用性预筛**。

        注意这只是筛子不是判决：说明书要求「检索到历史案例后，先验证适用条件，
        再决定是否复用其中的诊断步骤」。所以 ``experience`` 模块在拿到高分案例后
        仍然会逐条检查 ``applicability``（见 ``ExperienceCase.applicable_to``）。
        """
        score = 0.0
        weight = 0.0

        # 故障类别：权重最高，类别不同基本不该复用
        weight += 3.0
        if self.failure_category and self.failure_category == other.failure_category:
            score += 3.0

        # 错误码：有交集即得分（Jaccard）
        weight += 2.0
        score += 2.0 * _jaccard(set(self.error_codes), set(other.error_codes))

        # 日志模式
        weight += 1.5
        score += 1.5 * _jaccard(set(self.log_patterns), set(other.log_patterns))

        # 软件版本：不同版本往往结论不可迁移，给 1.0 的权重但不做硬否决 ——
        # 版本差异到底影响不影响，由 ExperienceCase.applicable_to 判定
        weight += 1.0
        if self.software_version and self.software_version == other.software_version:
            score += 1.0

        # 失败阶段 & 用例类型
        weight += 1.0
        if self.failed_stage and self.failed_stage == other.failed_stage:
            score += 0.5
        if self.test_case_kind and self.test_case_kind == other.test_case_kind:
            score += 0.5

        return score / weight if weight else 0.0


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


class FailureContext(BaseModel):
    """§3.1 统一的故障上下文。

    **不要只向排障 Agent 传入一段日志** —— 说明书的第一条要求就是构建结构化上下文，
    否则排障智能体会退化成普通的日志问答 Agent。
    """

    task_id: str
    test_case_id: str
    failure_type: str = FailureCategory.UNKNOWN.value
    expected_result: dict[str, Any] = Field(default_factory=dict)
    actual_result: dict[str, Any] = Field(default_factory=dict)
    software_version: str = ""
    test_config: dict[str, Any] = Field(default_factory=dict)
    environment_state: dict[str, Any] = Field(default_factory=dict)
    execution_trace: list[TraceEvent] = Field(default_factory=list)
    raw_logs: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    related_cases: list[str] = Field(default_factory=list)
    observed_at: datetime = Field(default_factory=utcnow)

    # 上下文自带的「已经有结果」的检查 —— 供 §6.2 模式 B（模板裁剪）复用。
    # 值里必须带 collected_at / version，否则无法判断能否复用。
    cached_checks: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # ---- 便捷视图 ----------------------------------------------------
    @property
    def failed_stage(self) -> str:
        """最后一个失败事件所属的执行阶段。"""
        for event in reversed(self.execution_trace):
            if event.failed:
                return event.skill or event.agent or "unknown"
        return ""

    @property
    def error_codes(self) -> list[str]:
        seen: list[str] = []
        for event in self.execution_trace:
            if event.error_code and event.error_code not in seen:
                seen.append(event.error_code)
        return seen

    @property
    def expected_actual_diff(self) -> dict[str, Any]:
        """预期与实际的差集 —— 「确定失败现象」这一步的确定性输入。"""
        diff: dict[str, Any] = {}
        for key, expected in self.expected_result.items():
            actual = self.actual_result.get(key)
            if actual != expected:
                diff[key] = {"expected": expected, "actual": actual}
        for key, actual in self.actual_result.items():
            if key not in self.expected_result:
                diff[key] = {"expected": None, "actual": actual}
        return diff

    def signature(self) -> FailureSignature:
        """构造故障指纹（§8.3 检索维度）。"""
        return FailureSignature(
            error_codes=self.error_codes,
            log_patterns=_extract_log_patterns(self.raw_logs),
            failure_category=self.failure_type,
            software_version=self.software_version,
            test_case_kind=self.test_case_id.split("_")[0] if self.test_case_id else "",
            failed_stage=self.failed_stage,
            environment=dict(self.environment_state),
        )

    def validate_minimum(self) -> list[str]:
        """§9 ``INVALID_INPUT`` 的判据：缺必要输入就无法开展有效诊断。

        返回缺失项列表，空列表表示可以开诊。
        """
        missing: list[str] = []
        if not self.task_id:
            missing.append("task_id")
        if not self.test_case_id:
            missing.append("test_case_id")
        if not self.expected_result and not self.actual_result:
            missing.append("expected_result/actual_result（无法确定失败现象）")
        return missing


_LOG_PATTERN_MARKERS = (
    "ERR_", "FATAL", "TIMEOUT", "ASSERT", "MISMATCH", "NOT_READY",
    "UNBOUND", "ABORT", "RESET", "RETRY_EXHAUSTED",
)


def _extract_log_patterns(logs: list[str]) -> list[str]:
    """从原始日志里抽出**结构化**日志模式（形如 ``ERR_XXX`` 的标记）。

    只取标记不取整行：整行日志含时间戳与随机 ID，直接当指纹会导致
    「同一故障永远检索不到自己」。
    """
    found: list[str] = []
    for line in logs:
        for token in line.replace("[", " ").replace("]", " ").split():
            cleaned = token.strip(",:;()<>\"'")
            if any(cleaned.startswith(marker) for marker in _LOG_PATTERN_MARKERS):
                if cleaned not in found:
                    found.append(cleaned)
    return found


# ======================================================================
# 三、§3.1 证据分层
# ======================================================================
class Evidence(BaseModel):
    """§3.1 证据 —— 原始证据与模型推断必须分开保存。

    说明书给的四层对照：

    ================== ==========================================
    层次                例子
    ================== ==========================================
    RAW（原始日志）     实际出现了某个错误码
    OBSERVED（观测事实） 环境检查接口返回设备不可用
    INFERRED（根因假设） 设备不可用可能导致测试失败
    VERIFIED（验证结论） 设备恢复后，复测是否成功
    ================== ==========================================

    ``derived_from`` 强制非空（对 INFERRED / VERIFIED 而言）是**结构性约束**：
    任何一条推断都必须指回它依据的原始证据，杜绝「凭空生成的事实」。
    """

    evidence_id: str = Field(default_factory=lambda: new_id("EV"))
    kind: EvidenceKind
    source: str                                  # 产出它的 skill / 模块
    claim: str                                   # 一句话结论（人可读）
    payload: dict[str, Any] = Field(default_factory=dict)
    ref: str | None = None                    # artifact:// 引用（原始日志用）
    collected_at: datetime = Field(default_factory=utcnow)
    derived_from: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_provenance(self) -> Evidence:
        if self.kind in (EvidenceKind.INFERRED, EvidenceKind.VERIFIED):
            if not self.derived_from:
                raise ValueError(
                    f"{self.kind.value} 证据必须通过 derived_from 指回原始证据：{self.claim!r}"
                )
        return self

    @property
    def is_raw(self) -> bool:
        return self.kind in (EvidenceKind.RAW, EvidenceKind.OBSERVED)


class EvidenceDraft(BaseModel):
    """工具产出的证据**草稿**。

    工具不直接造 :class:`Evidence`，而是交草稿给证据库统一编号入库 ——
    这样「谁生成了 ID」「谁负责溯源校验」只有一处实现，不会出现
    「某个工具绕开证据库自己拼了一条证据」。
    """

    kind: EvidenceKind
    claim: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ref: str | None = None


class ToolObservation(BaseModel):
    """一次诊断工具调用的返回。

    ``outcome`` 是**工具给出的、非模型判断的**结论 —— 假设更新与信息增益都以它为准。
    工具附带 ``evidence`` 草稿，由证据库统一编号入库并强制溯源。
    """

    skill: str
    outcome: Observation
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    ref: str | None = None
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    duration_ms: int = 0


# ======================================================================
# 五、§5.1 根因假设
# ======================================================================
class Hypothesis(BaseModel):
    """§5.1 假设记录 —— 说明书要求为每个假设记录的字段一个不少。

    ``key`` 是内部稳定键（``H_ENV`` / ``H_CONFIG``…），用于对齐动作池里的似然表。
    模型的自由文本落在 ``description``，两者不能混用：似然表是**确定性查表**，
    自由文本一旦被当成键，信息增益就变成了「猜词游戏」。
    """

    hypothesis_id: str = Field(default_factory=lambda: new_id("H"))
    key: str
    description: str
    category: FailureCategory = FailureCategory.UNKNOWN
    status: HypothesisStatus = HypothesisStatus.PENDING
    prior: float = 0.2
    posterior: float = 0.2
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    # 真正**推动**过本假设可信度的检查（|Δposterior| ≥ 阈值）。
    # 结论门要求领跑假设至少有一次这样的检查 —— 否则「没被反驳」会被误当「被支持」。
    discriminating_checks: list[str] = Field(default_factory=list)
    provenance: str = ""
    history: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def active(self) -> bool:
        """是否仍参与诊断规划（被排除或已支持的都不再需要证据）。"""
        return self.status in (HypothesisStatus.PENDING, HypothesisStatus.UNDECIDED)


# ======================================================================
# 四、§4.2 故障分类结果
# ======================================================================
class FailureClassification(BaseModel):
    """§4.2 分类结果。``confidence`` 是估计值，**不是校准过的真实概率**。

    说明书原文：「不应直接视为经过校准的真实概率。如果需要使用它做自动恢复决策，
    应该先利用历史标注样本验证置信度与实际正确率的对应关系。」
    因此本 Demo 里 confidence 只用于排序与展示，恢复决策一律走风险分级 + 证据门。
    """

    primary_category: FailureCategory = FailureCategory.UNKNOWN
    secondary_categories: list[FailureCategory] = Field(default_factory=list)
    candidate_causes: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)
    decided_by: str = "rule"          # rule / llm / rule+llm
    rationale: str = ""


# ======================================================================
# 五、§5.3 诊断动作声明
# ======================================================================
class ActionSpec(BaseModel):
    """§6.1 诊断动作池里的一条声明。

    说明书要求每个 Skill 都声明输入输出 Schema、前置条件、权限、超时、资源需求、
    是否可并行、是否有副作用、是否幂等 ——「这样 Planner 才能知道哪些动作可以执行，
    而不是仅根据 Skill 名称猜测」。

    ``likelihoods`` 是 §5.3 信息增益的燃料：

        likelihoods[hypothesis_key][observation] = P(observation | hypothesis)

    没有这张表，IG 就无从算起；有了它，规划器就能回答
    「哪个检查最能区分当前这几个候选根因」，而不是「哪个工具看起来最相关」。
    """

    name: str
    description: str = ""
    kind: str = "diagnostic"                  # diagnostic / recovery
    tier: RiskTier = RiskTier.R0
    side_effect: str = "none"                 # none / read / mutate
    idempotent: bool = True
    timeout_seconds: float = 5.0
    max_attempts: int = 2
    cost: float = 1.0                          # 归一化执行成本
    risk: float = 0.0                          # 潜在风险（0~1）
    resource: str | None = None             # 互斥资源名（如 "environment"）
    produces: list[str] = Field(default_factory=list)
    permission: str | None = None           # 需要的权限
    preconditions: list[str] = Field(default_factory=list)
    likelihoods: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: 该动作**可能产生**的观测结果。信息增益按这个集合求期望，
    #: 似然表里没提到的假设视为「无信息」（等概率），而不是「不可能」。
    outcomes: list[Observation] = Field(
        default_factory=lambda: [Observation.ANOMALY, Observation.NORMAL]
    )

    def declared_outcomes(self) -> list[Observation]:
        return list(self.outcomes)

    def likelihood(self, hypothesis_key: str, observation: Observation) -> float | None:
        """查表。返回 ``None`` 表示**该动作对这个假设没有信息量**。

        这个区分很重要：不能把「查不到」当成 0（那是强反驳），也不能当成 1（那是无信息）。
        调用方必须显式处理 ``None``。
        """
        row = self.likelihoods.get(hypothesis_key)
        if not row:
            return None
        return row.get(observation.value)


# ======================================================================
# 六、§6.3 结构化节点计划
# ======================================================================
class PlanNode(BaseModel):
    """§6.3 计划里的一个节点。**模型只输出这种结构，不输出可执行代码。**"""

    id: str
    skill: str
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    # 模式 B 裁剪时：指向被复用的旧节点/缓存项，执行器会跳过实际调用
    reused_from: str | None = None


class DiagnosticPlan(BaseModel):
    """§6.3 一份诊断计划（候选 DAG）。"""

    plan_id: str = Field(default_factory=lambda: new_id("PLAN"))
    mode: PlanMode = PlanMode.DYNAMIC
    nodes: list[PlanNode] = Field(default_factory=list)
    rationale: str = ""
    template_id: str | None = None
    proposed_by: str = "planner"        # planner / llm

    def node_map(self) -> dict[str, PlanNode]:
        return {node.id: node for node in self.nodes}

    def skills(self) -> list[str]:
        return [node.skill for node in self.nodes]


class ValidationIssue(BaseModel):
    """§6.3 校验问题。``severity=blocking`` 才会拦下计划。"""

    code: str
    severity: str = "blocking"      # info / warning / blocking
    node_id: str | None = None
    message: str = ""


class ValidationReport(BaseModel):
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def blocking(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "blocking"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.blocking


class NodeResult(BaseModel):
    """§6.3 节点级执行结果（节点级状态、超时、重试、检查点都挂在这里）。"""

    node_id: str
    skill: str
    status: NodeStatus = NodeStatus.PENDING
    observation: Observation | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    attempts: int = 1
    duration_ms: int = 0
    error: str | None = None
    reused_from: str | None = None
    skipped_reason: str | None = None

    @property
    def produced_observation(self) -> bool:
        return self.status is NodeStatus.SUCCEEDED and self.observation is not None


class TraceAnalysis(BaseModel):
    """§12 Q1「如何从执行轨迹中识别真正的故障根因」的分析结果。"""

    phenomenon_node: str | None = None      # 报错的地方（受害者）
    suspect_nodes: list[str] = Field(default_factory=list)   # 声称成功、实际没做到
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    dependency_edges: list[tuple[str, str]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def phenomenon_is_root(self) -> bool:
        """现象节点是否就是根因节点（没有找到更上游的疑似节点）。"""
        return not self.suspect_nodes


# ======================================================================
# 七、§7.1 受控恢复
# ======================================================================
class RecoveryDecision(BaseModel):
    """§7.2 恢复前验证门的输出。"""

    action: str | None = None
    tier: RiskTier = RiskTier.R0
    approved: bool = False
    requires_approval: bool = False
    blocked_reason: str | None = None
    matched_evidence: list[str] = Field(default_factory=list)
    rationale: str = ""


class RecoveryOutcome(BaseModel):
    """一次恢复 + 复测的完整记录（§7 要求保留操作记录与前后快照）。"""

    attempt: int
    action: str
    tier: RiskTier
    executed: bool
    before_snapshot: dict[str, Any] = Field(default_factory=dict)
    after_snapshot: dict[str, Any] = Field(default_factory=dict)
    verified: bool = False                 # 恢复动作本身是否达效（环境恢复正常）
    rerun_passed: bool | None = None    # 复测结果
    note: str = ""


# ======================================================================
# 九、§9 终止状态与预算
# ======================================================================
class Budget(BaseModel):
    """§10.3 第 6 条：循环必须受最大步数、时间、Token、工具调用和恢复次数约束。"""

    max_steps: int = 6
    max_tool_calls: int = 12
    max_seconds: float = 300.0
    max_recovery_attempts: int = 2
    max_nodes_per_plan: int = 6
    max_conclusion_retries: int = 1


class BudgetUsage(BaseModel):
    steps: int = 0
    tool_calls: int = 0
    elapsed_seconds: float = 0.0
    recovery_attempts: int = 0

    def exhausted(self, budget: Budget) -> str | None:
        """返回被耗尽的资源名；``None`` 表示还有余量。"""
        if self.steps >= budget.max_steps:
            return "max_steps"
        if self.tool_calls >= budget.max_tool_calls:
            return "max_tool_calls"
        if self.elapsed_seconds >= budget.max_seconds:
            return "max_seconds"
        if self.recovery_attempts >= budget.max_recovery_attempts:
            return "max_recovery_attempts"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "recovery_attempts": self.recovery_attempts,
        }


class DiagnosisConclusion(BaseModel):
    """§9 最终诊断结论 —— 排障任务的产物。"""

    task_id: str
    terminal_state: TerminalState
    root_cause: str | None = None
    root_cause_key: str | None = None
    confidence: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)
    classification: FailureClassification | None = None
    trace_analysis: TraceAnalysis | None = None
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    diagnostic_path: list[str] = Field(default_factory=list)
    recovery: list[RecoveryOutcome] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    unresolved_reason: str | None = None
    plan_modes: list[str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.terminal_state is TerminalState.RESOLVED

    def to_report(self) -> str:
        """生成结构化排障报告（阶段一的核心交付物）。"""
        lines = [
            f"任务      : {self.task_id}",
            f"终止状态  : {self.terminal_state.value}",
            f"根因      : {self.root_cause or '（未定位）'}",
            f"置信度    : {self.confidence:.2f}",
            f"证据条数  : {len(self.evidence_ids)}",
        ]
        if self.verification:
            lines.append(f"结果验证  : {self.verification}")
        if self.unresolved_reason:
            lines.append(f"未定位原因: {self.unresolved_reason}")
        return "\n".join(lines)


# ======================================================================
# 八、§8.1 故障经验库
# ======================================================================
class VerifiedRootCause(BaseModel):
    """§8.1 经过**验证**的根因（未验证的一律进 CANDIDATE，不进这里）。"""

    key: str
    description: str
    evidence_ids: list[str] = Field(default_factory=list)


class ExperienceCase(BaseModel):
    """§8.1 故障经验的数据结构。"""

    case_id: str = Field(default_factory=lambda: new_id("CASE"))
    status: ExperienceStatus = ExperienceStatus.CANDIDATE
    failure_signature: FailureSignature = Field(default_factory=FailureSignature)
    symptoms: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    verified_root_cause: VerifiedRootCause | None = None
    diagnostic_steps: list[str] = Field(default_factory=list)
    recovery_actions: list[str] = Field(default_factory=list)
    verification_result: dict[str, Any] = Field(default_factory=dict)
    applicability: dict[str, Any] = Field(default_factory=dict)
    effective: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    reused_count: int = 0

    @property
    def signature_hash(self) -> str:
        return stable_hash(
            {
                "category": self.failure_signature.failure_category,
                "codes": sorted(self.failure_signature.error_codes),
                "patterns": sorted(self.failure_signature.log_patterns),
                "root_cause": self.verified_root_cause.key if self.verified_root_cause else None,
            }
        )

    def applicable_to(self, ctx: FailureContext) -> tuple[bool, str]:
        """§8.3 适用条件校验 —— **不能因为描述相似就直接套用恢复操作。**

        返回 ``(是否适用, 判据说明)``。
        """
        versions = self.applicability.get("software_versions")
        if versions and ctx.software_version and ctx.software_version not in versions:
            return False, (
                f"软件版本不匹配：案例适用 {versions}，当前 {ctx.software_version}"
            )
        stages = self.applicability.get("stages")
        if stages and ctx.failed_stage not in stages:
            return False, f"失败阶段不匹配：案例适用 {stages}，当前 {ctx.failed_stage}"
        if self.status is ExperienceStatus.FAILED:
            return False, "这是失败经验：仅用于规避，不可复用其恢复动作"
        return True, "适用条件满足"
