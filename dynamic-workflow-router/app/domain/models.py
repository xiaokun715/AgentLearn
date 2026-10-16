"""动态 Workflow 路由系统的领域模型。

对齐《第十七章：动态 Workflow 与 Agent 路由设计说明书》：
- 四、§4.1 子 Agent 声明          -> :class:`SubAgentSpec`
- 五、§5.2 意图的结构              -> :class:`Intent`
- 六、§6.5 可解释的路由决策        -> :class:`RouteCandidate` / :class:`RoutingDecision`
- 七、§7   运行请求                -> :class:`RunRequest`
- 八、§8   Workflow 结构           -> :class:`WorkflowStep` / :class:`WorkflowTemplate`
- 十一、§11 模板标签               -> :class:`TemplateTags` / :class:`TemplateStats`
- 十二、§12 保存与丢弃             -> :class:`RunResult`
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from .enums import (
    AgentHealth,
    ExecutionMode,
    IntentStatus,
    RiskLevel,
    RouteTarget,
    StepStatus,
    TemplateFate,
    TemplateSource,
    TemplateState,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def stable_hash(payload: Any) -> str:
    """稳定哈希：键排序 + 非 ASCII 不转义。

    模板的「结构指纹」用它 —— 同一份 DAG 换个键顺序必须得到同一个值，
    否则那次去重就白做了。
    """
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# ======================================================================
# 四、§4.1 子 Agent 声明
# ======================================================================
class SubAgentSpec(BaseModel):
    """池子里的一个子 Agent。

    **能力标签比名字重要**（§4.2）。路由引擎不应该认识 ``log_analyzer``
    这个名字，它只应该认识 ``log_analysis`` 这个能力 —— 否则每次重命名
    子 Agent 都要改一遍路由规则。

    ``reads`` / ``writes`` 是数据流契约：composer 靠它们排出依赖关系，
    validator 靠它们校验数据流是否闭合（§8.3）。
    """

    name: str
    description: str
    domain: str
    capabilities: list[str] = Field(default_factory=list)
    #: 需要从黑板书读哪些键
    reads: list[str] = Field(default_factory=list)
    #: 会往黑板书写哪些键
    writes: list[str] = Field(default_factory=list)
    #: 相对成本（0~10），用于路由评分与预算
    cost: float = 1.0
    #: 单次执行耗时（毫秒），用于估算端到端延迟
    latency_ms: int = 200
    risk: RiskLevel = RiskLevel.LOW
    #: 有副作用的 Agent 不允许在「自动编排」里被悄悄插进去
    side_effect: bool = False
    health: AgentHealth = AgentHealth.HEALTHY
    #: 健康度打折系数（DEGRADED 时 < 1）
    health_factor: float = 1.0

    @property
    def usable(self) -> bool:
        return self.health is not AgentHealth.OFFLINE


# ======================================================================
# 五、§5.2 意图
# ======================================================================
class Intent(BaseModel):
    """一次意图识别的结果。

    §5.2 的核心主张：**意图不只是个名字**。只有名字的话，下游要么
    重新解析一遍 query，要么被迫在路由规则里塞满业务知识。
    所以这里把「要什么」结构化出来：需要哪些能力、大概几步、
    风险多高、要不要多步编排。
    """

    name: str
    status: IntentStatus = IntentStatus.RESOLVED
    confidence: float = 0.0
    #: 从 query 里抽出来的槽位（文件名、模块名、时间范围……）
    entities: dict[str, Any] = Field(default_factory=dict)
    #: 完成这个意图**必须**覆盖的能力集合 —— 路由和编排都围绕它展开
    required_capabilities: list[str] = Field(default_factory=list)
    #: 估计需要几步。1 表示单步即可，>1 表示天然是编排任务
    estimated_steps: int = 1
    risk: RiskLevel = RiskLevel.LOW
    #: 除了主意图之外的次候选（用于澄清与「顺便也做一下」的场景）
    alternates: list[str] = Field(default_factory=list)
    decided_by: str = "rule"        # rule / llm / unknown
    rationale: str = ""
    raw_query: str = ""

    @property
    def needs_orchestration(self) -> bool:
        """是否需要多步编排。

        判据是 ``estimated_steps > 1`` 而不是「模型觉得复杂」——
        后者会让同一句话今天走直连、明天走编排，无法回归。
        """
        return self.estimated_steps > 1

    @property
    def actionable(self) -> bool:
        return self.status is IntentStatus.RESOLVED and self.confidence >= 0.3


# ======================================================================
# 八、§8 Workflow 结构
# ======================================================================
class WorkflowStep(BaseModel):
    """模板里的一个步骤。

    ``input_map`` 是「参数名 -> 黑板书键」的映射。它看起来啰嗦，
    但它是**数据流能被校验**的唯一原因（§8.3）：没有它，
    validator 只能检查「有没有环」，检查不了「这一步要的东西前面到底产出了没有」。
    """

    id: str
    agent: str
    depends_on: list[str] = Field(default_factory=list)
    #: 该步骤需要的黑板书键（必须由上游产出，或由 query 直接提供）
    needs: list[str] = Field(default_factory=list)
    #: 该步骤产出的黑板书键
    produces: list[str] = Field(default_factory=list)
    #: 可选步骤：失败或超预算时可以跳过，不阻断整条链
    optional: bool = False
    note: str = ""


class TemplateTags(BaseModel):
    """§11 模板标签 —— 需求 4 的落点。

    标签不只是元数据，它是**模板库能被治理的唯一原因**（§11.4）。
    没有「谁生成的、什么时候、跑过几次、成功率多少」，
    模板库就只是一个会把相似 DAG 存一百遍的垃圾场。
    """

    # ---- 溯源标签：谁生成的、什么时候 ------------------------------
    author: str                      # 生成人（用户名，或 "composer" / "seed"）
    created_at: datetime = Field(default_factory=utcnow)
    source: TemplateSource = TemplateSource.LLM_GENERATED
    # ---- 描述标签：它属于哪个域、对应哪个意图 ----------------------
    domain: str = "general"
    intent: str = ""
    risk: RiskLevel = RiskLevel.LOW
    labels: list[str] = Field(default_factory=list)
    # ---- 治理标签：能不能被执行 --------------------------------
    state: TemplateState = TemplateState.DRAFT
    #: 被取代时指向新版本
    superseded_by: str | None = None
    #: 这份模板是按哪个 query 生成的（便于人工审查）
    origin_query: str = ""

    def matches_labels(self, wanted: list[str]) -> bool:
        return set(wanted).issubset(set(self.labels))


class TemplateStats(BaseModel):
    """§11.3 运行统计 —— 让「成熟模板优先」有数据支撑，而不是靠感觉。"""

    runs: int = 0
    successes: int = 0
    failures: int = 0
    total_latency_ms: int = 0
    last_used_at: datetime | None = None
    #: 被路由引擎拒绝过多少次（说明这个模板的适用面被高估了）
    rejected: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0

    @property
    def avg_latency_ms(self) -> int:
        return int(self.total_latency_ms / self.runs) if self.runs else 0

    def record(self, ok: bool, latency_ms: int) -> None:
        self.runs += 1
        self.successes += 1 if ok else 0
        self.failures += 0 if ok else 1
        self.total_latency_ms += latency_ms
        self.last_used_at = utcnow()


class WorkflowTemplate(BaseModel):
    """一份可执行的 Workflow 模板。"""

    template_id: str = Field(default_factory=lambda: new_id("wf"))
    name: str
    description: str = ""
    steps: list[WorkflowStep] = Field(default_factory=list)
    tags: TemplateTags
    stats: TemplateStats = Field(default_factory=TemplateStats)
    version: int = 1
    #: 上一版模板的 id（版本演进链）
    based_on: str | None = None

    # ------------------------------------------------------------------
    def step_map(self) -> dict[str, WorkflowStep]:
        return {step.id: step for step in self.steps}

    def agents_used(self) -> list[str]:
        return [step.agent for step in self.steps]

    def estimated_cost(self, pool: AgentPoolLike) -> float:
        return sum(pool.cost_of(step.agent) for step in self.steps)

    def estimated_latency_ms(self, pool: AgentPoolLike) -> int:
        """关键路径耗时（并行分支取最大值，不是求和）。"""
        return _critical_path_ms(self, pool)

    @property
    def structure_hash(self) -> str:
        """§12.2 结构指纹 —— 去重用它。

        刻意**只哈希结构与数据流**，不含 name / description / tags：
        两个人分别生成出同一份 DAG，那就是同一份 DAG，
        不该在库里存两遍。
        """
        return stable_hash([
            {
                "agent": step.agent,
                "needs": sorted(step.needs),
                "produces": sorted(step.produces),
                "optional": step.optional,
            }
            for step in self.steps
        ])

    def max_risk(self, pool: AgentPoolLike) -> RiskLevel:
        levels = [pool.risk_of(step.agent) for step in self.steps]
        return max(levels, key=lambda level: level.order) if levels else RiskLevel.LOW

    def estimated_cost_total(self, pool: AgentPoolLike) -> float:
        return round(sum(pool.cost_of(step.agent) for step in self.steps), 2)


class AgentPoolLike(BaseModel):
    """仅用于类型提示：避免 models 反向依赖 pool 包。"""

    model_config = {"arbitrary_types_allowed": True}


def _critical_path_ms(template: WorkflowTemplate, pool: AgentPoolLike) -> int:
    """关键路径：DAG 上最长的一条路径。

    为什么不能简单求和 —— 编排的意义就是让互不依赖的步骤并行，
    求和会把并行的收益算没，于是「编排比直连慢」这个结论就永远是假的。
    """
    by_id = template.step_map()
    memo: dict[str, int] = {}

    def walk(step_id: str) -> int:
        if step_id in memo:
            return memo[step_id]
        step = by_id.get(step_id)
        if step is None:
            return 0
        own = pool.latency_of(step.agent)
        upstream = max((walk(dep) for dep in step.depends_on), default=0)
        memo[step_id] = own + upstream
        return memo[step_id]

    return max((walk(step.id) for step in template.steps), default=0)


# ======================================================================
# 六、§6.5 路由决策
# ======================================================================
class RouteCandidate(BaseModel):
    """一个候选终点及其评分明细。

    **明细必须能被打出来**（§6.5）：路由是一个会被人反复质疑的决策 ——
    「为什么这次走了编排而不是直连？」必须有答案，而且答案不能是
    「模型觉得」。
    """

    kind: RouteTarget
    name: str                    # agent 名 或 template id
    label: str                   # 人读的名字
    score: float = 0.0
    coverage: float = 0.0        # 能力覆盖度 0~1
    intent_match: float = 0.0
    reliability: float = 0.0
    cost: float = 0.0
    latency_ms: int = 0
    risk: RiskLevel = RiskLevel.LOW
    steps: int = 1
    blocked_reason: str | None = None

    def explain(self) -> str:
        return (
            f"{self.label:<26} score={self.score:6.3f} "
            f"覆盖={self.coverage:.2f} 意图={self.intent_match:.2f} "
            f"可靠={self.reliability:.2f} 成本={self.cost:.1f} "
            f"步数={self.steps} 风险={self.risk.value}"
        )


class RoutingDecision(BaseModel):
    """路由引擎的结论。"""

    target: RouteTarget
    chosen: str | None = None     # agent 名 或 template id
    chosen_label: str = ""
    score: float = 0.0
    candidates: list[RouteCandidate] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    rationale: str = ""
    forced: bool = False             # 是否由 ExecutionMode 强制

    @property
    def needs_compose(self) -> bool:
        return self.target is RouteTarget.COMPOSE_NEW


# ======================================================================
# 七、§7 运行请求
# ======================================================================
class RunRequest(BaseModel):
    """一次用户请求。"""

    query: str
    mode: ExecutionMode = ExecutionMode.AUTO
    #: 发起人 —— 会写进生成出来的模板标签里（需求 4）
    actor: str = "anonymous"
    #: 用户希望给生成的模板打上的标签
    labels: list[str] = Field(default_factory=list)
    #: 期望的意图（用于演示「强制指定」，可选）
    forced_intent: str | None = None
    max_steps: int = 8
    max_cost: float = 12.0
    max_risk: RiskLevel = RiskLevel.MEDIUM


# ======================================================================
# 十、§10 执行
# ======================================================================
class StepResult(BaseModel):
    id: str
    agent: str
    status: StepStatus = StepStatus.PENDING
    started_ms: int = 0
    finished_ms: int = 0
    attempts: int = 1
    produced: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @property
    def duration_ms(self) -> int:
        return max(0, self.finished_ms - self.started_ms)


class RunResult(BaseModel):
    """一次完整运行的产出。"""

    run_id: str = Field(default_factory=lambda: new_id("run"))
    request: RunRequest
    intent: Intent
    decision: RoutingDecision
    #: 实际执行的模板（直连时会临时包一个单步模板，保证执行路径只有一条）
    template: WorkflowTemplate | None = None
    step_results: list[StepResult] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    answer: str = ""
    #: 编排本身的耗时（生成 + 校验）—— 直连与命中模板时是 0
    compose_ms: int = 0
    total_ms: int = 0
    #: 默认 NONE：**只有现场生成**才产生待用户决策的模板。
    #: 直连与复用没有「新生成」这件事，把它们也塞进保存/丢弃流程，
    #: 会让用户面对一堆不值得决策的决策。
    template_fate: TemplateFate = TemplateFate.NONE
    notes: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(
            item.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED)
            for item in self.step_results
        )

    @property
    def path(self) -> str:
        return self.decision.target.value


# ======================================================================
# 九、§9 校验
# ======================================================================
class ValidationIssue(BaseModel):
    code: str
    severity: str = "blocking"      # info / warning / blocking
    step_id: str | None = None
    message: str = ""


class ValidationReport(BaseModel):
    issues: list[ValidationIssue] = Field(default_factory=list)
    #: 校验过程中自动修复了哪些问题
    repairs: list[str] = Field(default_factory=list)

    @property
    def blocking(self) -> list[ValidationIssue]:
        return [item for item in self.issues if item.severity == "blocking"]

    @property
    def ok(self) -> bool:
        return not self.blocking
