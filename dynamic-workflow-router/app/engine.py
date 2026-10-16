"""路由引擎的门面 —— 把「意图 → 路由 → 编排 → 执行 → 保存/丢弃」串成一个闭环。

对应说明书 §3 的总体架构。它自己不实现任何判断，只负责**按正确的顺序
调用正确的部件**，以及在每一步留下可解释的记录。

一次运行的完整链路：

    query
      │
      ├─ ① 意图识别        -> Intent（名字 + 能力集合 + 步数估计 + 置信度）
      │
      ├─ ② 路由            -> RoutingDecision（五种终点之一）
      │
      ├─ ③ 分发
      │     ├─ CLARIFY / REJECT     -> 直接返回，不执行
      │     ├─ DIRECT_AGENT         -> 包成单步模板执行（无编排成本）
      │     ├─ EXISTING_TEMPLATE    -> 取模板执行（无编排成本，有历史统计）
      │     └─ COMPOSE_NEW          -> 生成 → 校验 → 修复 → 执行（有编排成本）
      │
      ├─ ④ 执行            -> 黑板书 + 步骤轨迹（关键路径耗时）
      │
      └─ ⑤ 模板处置        -> 现场生成的模板进入 PENDING，等用户 SAVE / DISCARD

**只有 COMPOSE_NEW 会产生待处置的模板**：直连和复用都没有「新生成」这件事，
把它们也塞进「保存/丢弃」流程，会让用户面对一堆不值得决策的决策。
"""
from __future__ import annotations

from dataclasses import dataclass

from .domain.enums import (
    IntentStatus,
    RouteTarget,
    StepStatus,
    TemplateFate,
    TemplateSource,
    TemplateState,
)
from .domain.models import (
    Intent,
    RunRequest,
    RunResult,
    TemplateTags,
    WorkflowStep,
    WorkflowTemplate,
)
from .domain.policy import SAVE_POLICY
from .intent.recognizer import IntentRecognizer
from .pool.agents import AgentPool
from .pool.templates import SaveOutcome, TemplateStore
from .routing.engine import RoutingEngine
from .workflow import composer as compose_mod
from .workflow.runtime import ExecutionTrace, RuntimeOptions, WorkflowRuntime
from .workflow.validator import WorkflowValidator


@dataclass
class RunContext:
    """运行过程中的中间状态，便于场景脚本逐步展示。"""

    intent: Intent
    result: RunResult
    compose_notes: list[str]
    compose_error: str | None = None


class WorkflowRouter:
    """门面。"""

    def __init__(
        self,
        *,
        pool: AgentPool,
        store: TemplateStore,
        recognizer: IntentRecognizer,
        router: RoutingEngine,
        validator: WorkflowValidator,
        runtime: WorkflowRuntime,
        planner: compose_mod.CapabilityPlanner,
        llm_composer: compose_mod.LLMComposer | None = None,
        llm=None,
    ) -> None:
        self.pool = pool
        self.store = store
        self.recognizer = recognizer
        self.router = router
        self.validator = validator
        self.runtime = runtime
        self.planner = planner
        self.llm_composer = llm_composer
        self.llm = llm
        #: 最近一次运行的中间状态，供演示脚本读取
        self.last_context: RunContext | None = None
        #: 最近一次编排的中间笔记（每个能力选了谁、补齐了什么）
        self._compose_notes: list[str] = []

    # ==================================================================
    # 主链路
    # ==================================================================
    def run(self, request: RunRequest, *, runtime_options: RuntimeOptions | None = None,
            use_llm_composer: bool = False) -> RunResult:
        # ---- ① 意图 ---------------------------------------------------
        intent = self.recognizer.recognize(request.query, forced=request.forced_intent)

        # ---- ② 路由 ---------------------------------------------------
        decision = self.router.route(intent, request)
        result = RunResult(request=request, intent=intent, decision=decision)
        ctx = RunContext(intent=intent, result=result, compose_notes=[])

        # ---- ③ 分发 ---------------------------------------------------
        if decision.target is RouteTarget.CLARIFY:
            result.notes.append("进入澄清分支，没有执行任何子 Agent")
            result.answer = self._clarify(request.query, decision)
            self.last_context = ctx
            return result

        if decision.target is RouteTarget.REJECT:
            result.notes.append("路由判定为超出能力边界")
            result.answer = f"当前 Agent 池无法处理这个请求：{decision.rationale}"
            self.last_context = ctx
            return result

        if decision.target is RouteTarget.DIRECT_AGENT:
            template = self._wrap_single_agent(decision.chosen, intent)
            result.notes.append(
                f"直连 {decision.chosen} —— 不产生模板，也就没有编排成本"
            )
            self._execute(result, template, intent, runtime_options)
            result.template = None      # 直连不暴露临时模板
            self.last_context = ctx
            return result

        if decision.target is RouteTarget.EXISTING_TEMPLATE:
            template = self.store.get(decision.chosen)
            if template is None:
                result.notes.append(f"模板 {decision.chosen} 已不存在，回退到现场编排")
                decision = decision.model_copy(update={"target": RouteTarget.COMPOSE_NEW,
                                                       "chosen": None})
                result.decision = decision
            else:
                result.notes.append(
                    f"复用模板 {template.template_id}（v{template.version}，"
                    f"已跑 {template.stats.runs} 次）—— 无编排成本"
                )
                result.template = template      # 复用的模板也要暴露出来，
                #                                 否则调用方拿不到「这次是按哪份 DAG 跑的」
                self._execute(result, template, intent, runtime_options)
                self.store.record_run(template.template_id, result.ok, result.total_ms)
                self.last_context = ctx
                return result

        # ---- COMPOSE_NEW ---------------------------------------------
        template, compose_ms, error = self._compose(
            intent, request, use_llm_composer=use_llm_composer
        )
        ctx.compose_error = error
        if template is None:
            result.notes.append(f"现场编排失败：{error}")
            result.answer = f"无法为这个请求生成可执行的编排：{error}"
            result.template_fate = TemplateFate.NONE
            self.last_context = ctx
            return result

        result.compose_ms = compose_ms
        result.template = template
        result.template_fate = TemplateFate.PENDING
        result.notes.append(
            f"现场生成 {len(template.steps)} 步模板（编排耗时 {compose_ms}ms）"
            "—— 运行后由用户决定保存还是丢弃"
        )
        self._execute(result, template, intent, runtime_options, count_toward_total=True)
        self.last_context = ctx
        return result

    # ==================================================================
    # 现场编排（§8）
    # ==================================================================
    def _compose(
        self, intent: Intent, request: RunRequest, *, use_llm_composer: bool
    ) -> tuple[WorkflowTemplate | None, int, str | None]:
        """返回 ``(模板, 编排耗时, 失败原因)``。"""
        notes: list[str] = []
        latency = compose_mod.COMPOSE_VALIDATE_LATENCY_MS

        # ---- 优先让模型提议（它是补充路径，不是主路径）-----------------
        template: WorkflowTemplate | None = None
        if use_llm_composer and self.llm_composer is not None:
            latency += compose_mod.COMPOSE_LLM_LATENCY_MS
            template, llm_notes = self.llm_composer.propose(intent)
            notes.extend(llm_notes)
            if template is not None:
                report = self.validator.validate(
                    template, intent=intent, request=request
                )
                if not report.ok:
                    notes.append(
                        "模型提议未通过校验，回退到确定性规划器："
                        + "；".join(f"[{i.code}] {i.message}" for i in report.blocking[:2])
                    )
                    template = None

        # ---- 回退到确定性规划器（主路径）------------------------------
        if template is None:
            try:
                template = self.planner.compose(intent)
            except compose_mod.ComposeError as exc:
                return None, latency, str(exc)
            notes.extend(self.planner.notes)

        # ---- 校验 + 修复（§8.4 / §9）----------------------------------
        template, repair_report = self.validator.repair(template)
        notes.extend(repair_report.repairs)
        report = self.validator.validate(template, intent=intent, request=request)
        if not report.ok:
            return None, latency, "；".join(
                f"[{i.code}] {i.message}" for i in report.blocking
            )

        # ---- 附上治理标签（§11）--------------------------------------
        template.tags.author = request.actor
        template.tags.created_at = template.tags.created_at
        template.tags.origin_query = request.query
        for label in request.labels:
            if label not in template.tags.labels:
                template.tags.labels.append(label)
        for label in SAVE_POLICY.default_labels:
            if label not in template.tags.labels:
                template.tags.labels.append(label)
        template.description = "；".join(notes[-3:]) or template.description

        self._compose_notes = notes
        return template, latency, None

    # ==================================================================
    # 执行（§10）
    # ==================================================================
    def _execute(
        self,
        result: RunResult,
        template: WorkflowTemplate,
        intent: Intent,
        options: RuntimeOptions | None,
        *,
        count_toward_total: bool = False,
    ) -> None:
        trace = self.runtime.execute(template, intent, options)
        result.step_results = trace.results
        result.context = trace.context
        # 编排耗时算进端到端总时长 —— 它是「第一次」的真实代价
        result.total_ms = trace.total_ms + (result.compose_ms if count_toward_total else 0)
        if count_toward_total:
            result.notes.append(
                f"关键路径执行 {trace.total_ms}ms + 编排 {result.compose_ms}ms "
                f"= {result.total_ms}ms"
            )
        else:
            result.notes.append(f"关键路径执行 {trace.total_ms}ms（无编排成本）")
        result.answer = self._answer_from(trace)

        # 现场生成的模板：把这次运行记进它自己的统计
        # —— 这是 §12.1「先跑过一次才允许入库」那条门的数据来源
        if count_toward_total and result.template is not None:
            result.template.stats.record(result.ok, result.total_ms)
            if result.template.tags.state is TemplateState.DRAFT:
                result.template.tags.risk = result.template.max_risk(self.pool)

    # ==================================================================
    # 模板处置（§12，需求 3 的后半段）
    # ==================================================================
    def decide_fate(
        self,
        result: RunResult,
        fate: TemplateFate,
        *,
        actor: str | None = None,
        labels: list[str] | None = None,
    ) -> SaveOutcome | None:
        """用户对现场生成的模板做出「保存 / 丢弃」的决定。

        **这是一个显式动作，不是自动行为**（§12.4）。理由是模板库的
        长期质量只能靠「每一次入库都有人负责」来保证 ——
        让系统自动保存每一次生成的模板，一周之后库里就是一堆
        同义、低质、没人敢用的 DAG。
        """
        template = result.template
        if template is None:
            return None

        if fate is TemplateFate.DISCARD:
            template.tags.state = TemplateState.BROKEN
            result.template_fate = TemplateFate.DISCARD
            return SaveOutcome(template, False, "DISCARDED", "用户选择丢弃，未入库")

        template.tags.author = actor or result.request.actor
        merged_labels = list(dict.fromkeys([*(labels or []), *template.tags.labels]))
        template.tags.labels = merged_labels
        outcome = self.store.add(template, require_verified=SAVE_POLICY.require_verified)
        result.template_fate = TemplateFate.SAVE if outcome.stored else TemplateFate.DISCARD
        return outcome

    # ==================================================================
    # 辅助
    # ==================================================================
    def _wrap_single_agent(self, agent_name: str, intent: Intent) -> WorkflowTemplate:
        """把直连包成单步模板 —— 让执行路径只有一条（§10）。

        路由有三条路，执行只有一种形状。这样运行时不需要知道
        「这次是直连还是编排」，步骤级重试/统计/轨迹也全都统一。
        """
        spec = self.pool.require(agent_name)
        return WorkflowTemplate(
            name=f"（直连）{agent_name}",
            description=spec.description,
            steps=[WorkflowStep(
                id="d1", agent=agent_name,
                needs=list(spec.reads), produces=list(spec.writes),
                note="直连子 Agent",
            )],
            tags=TemplateTags(
                author="router", source=TemplateSource.SEED,
                domain=spec.domain, intent=intent.name,
                risk=spec.risk, labels=["直连"], state=TemplateState.DRAFT,
                origin_query=intent.raw_query,
            ),
        )

    def _answer_from(self, trace: ExecutionTrace) -> str:
        ctx = trace.context
        for key in ("report", "summary", "trend", "hypotheses"):
            if key in ctx:
                value = ctx[key]
                if isinstance(value, list) and value:
                    return str(value[0])
                return str(value)
        parts = [k for k in ctx if not k.startswith("_")]
        return f"已产出：{', '.join(parts) or '（无）'}"

    def _clarify(self, query: str, decision) -> str:
        candidates = [item.label for item in decision.candidates]
        if self.llm is not None:
            return self.llm.clarification_question(query, candidates)
        return "能再说得具体一点吗？"


__all__ = [
    "WorkflowRouter",
    "RunContext",
    "ExecutionTrace",
    "IntentStatus",
    "StepStatus",
]
