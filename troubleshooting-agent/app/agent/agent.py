"""排障主 Agent —— 把前面所有模块串成一个闭环（说明书 §10.3 的决策循环）。

说明书给出的循环骨架是：

    def troubleshoot(failure_context):
        state = initialize_state(failure_context)
        persist(state)
        while not state.is_terminal():
            evidence = collect_available_evidence(state)
            trace = analyze_execution_trace(state)
            hypotheses = update_hypotheses(state, evidence, trace)
            if enough_evidence_for_conclusion(hypotheses, evidence):
                conclusion = build_conclusion(hypotheses, evidence); break
            actions = generate_diagnostic_actions(hypotheses, evidence)
            plan = planner.select(actions, state.budget)
            validate_plan(plan, state.permissions, state.resources)
            results = execute_ready_nodes(plan, state)
            state = update_state(state, results)
            if recovery_is_justified(state):
                require_authorization_if_needed(state)
                recovery_result = execute_recovery(state)
                state = verify_recovery_result(state, recovery_result)
        return finalize_diagnosis(state)

本文件是它的一份**可运行实现**。伪代码里那七条注意事项逐一落实如下：

1. ``enough_evidence_for_conclusion`` 不依赖 LLM —— 见
   :meth:`~app.hypothesis.manager.HypothesisManager.conclusion_ready` 的三道硬门；
2. ``validate_plan`` 由确定性代码执行 —— :class:`~app.planning.validator.WorkflowValidator`；
3. ``execute_ready_nodes`` 支持节点级状态/超时/重试/检查点 ——
   :class:`~app.execution.executor.WorkflowExecutor`；
4. ``execute_recovery`` 检查权限、幂等性和副作用 ——
   :class:`~app.recovery.controller.RecoveryController`；
5. ``persist`` 保证关键状态可恢复 —— :class:`~app.execution.executor.CheckpointStore`；
6. 循环受步数/时间/工具调用/恢复次数预算约束 —— :class:`~app.domain.models.Budget`；
7. 无法定位时输出 ``UNRESOLVED``，**不为结论强行选一个根因**。

还有一条本实现额外强调的边界：**轨迹是路标，检查是证据**。
轨迹分析（谁自称成功却没做到）只用来调整假设的**先验**，不计入结论门的证据账。
把路标计成证据，等于让「看起来很可疑」直接冒充「已经证实」。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..clock import Clock
from ..domain.enums import (
    ExperienceStatus,
    FailureCategory,
    PlanMode,
    TerminalState,
)
from ..domain.models import (
    Budget,
    BudgetUsage,
    DiagnosisConclusion,
    DiagnosticPlan,
    FailureClassification,
    FailureContext,
    RecoveryDecision,
    RecoveryOutcome,
    TraceAnalysis,
    ValidationReport,
)
from ..domain.policy import CONCLUSION
from ..evidence.store import EvidenceStore
from ..execution.executor import WorkflowExecutor
from ..experience.manager import ExperienceManager
from ..hypothesis.manager import HypothesisManager
from ..planning.actions import ActionPool
from ..planning.planner import DiagnosticPlanner
from ..planning.validator import WorkflowValidator
from ..recovery.controller import RecoveryController
from ..tools.base import ToolContext
from ..tracing.analyzer import TraceAnalyzer
from ..tracing.analyzer import summarize as summarize_trace

#: 疑似根因节点的 skill 名 → 假设键。用关键词而不是精确名，是因为各团队的
#: 阶段命名并不统一；真机上这张表应当来自平台自己的阶段元数据。
_SKILL_HYPOTHESIS_HINTS: tuple[tuple[str, str], ...] = (
    ("env", "H_ENV"),
    ("environment", "H_ENV"),
    ("config", "H_CONFIG"),
    ("param", "H_CONFIG"),
    ("case", "H_CASE"),
    ("expect", "H_CASE"),
    ("collect", "H_TOOL"),
    ("tool", "H_TOOL"),
    ("measure", "H_TOOL"),
)

#: 轨迹线索对先验的加权幅度（每命中一条）
TRACE_PRIOR_BOOST = 0.25


@dataclass
class DiagnosisState:
    """一次排障任务的完整状态（= 说明书里的 ``state``，可被 ``persist``）。"""

    ctx: FailureContext
    run_id: str
    trace: TraceAnalysis | None = None
    classification: FailureClassification | None = None
    manager: HypothesisManager | None = None
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    trace_evidence: list[str] = field(default_factory=list)
    diagnostic_path: list[str] = field(default_factory=list)
    plan_modes: list[str] = field(default_factory=list)
    plan_rationales: list[str] = field(default_factory=list)
    validation_reports: list[ValidationReport] = field(default_factory=list)
    executed_skills: set[str] = field(default_factory=set)
    attempted_recoveries: list[str] = field(default_factory=list)
    recoveries: list[RecoveryOutcome] = field(default_factory=list)
    recovery_decisions: list[RecoveryDecision] = field(default_factory=list)
    experience_notes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    terminal: TerminalState | None = None
    unresolved_reason: str | None = None


class TroubleshootingAgent:
    """排障主控 Agent：理解诊断状态、选择下一步动作。"""

    def __init__(
        self,
        *,
        pool: ActionPool,
        store: EvidenceStore,
        executor: WorkflowExecutor,
        planner: DiagnosticPlanner,
        validator: WorkflowValidator,
        recovery: RecoveryController,
        experience: ExperienceManager,
        tool_ctx: ToolContext,
        clock: Clock,
        budget: Budget | None = None,
        llm=None,
        use_llm_planner: bool = False,
    ) -> None:
        self.pool = pool
        self.store = store
        self.executor = executor
        self.planner = planner
        self.validator = validator
        self.recovery = recovery
        self.experience = experience
        self.tool_ctx = tool_ctx
        self.clock = clock
        self.budget = budget or Budget()
        self.llm = llm
        self.use_llm_planner = use_llm_planner
        self.analyzer = TraceAnalyzer()

    # ==================================================================
    # 主循环
    # ==================================================================
    def troubleshoot(
        self,
        ctx: FailureContext,
        *,
        run_id: str = "run",
        auto_approve: bool = False,
        use_experience: bool = True,
        use_templates: bool = True,
    ) -> DiagnosisConclusion:
        state = DiagnosisState(ctx=ctx, run_id=run_id)

        # ---- 步骤 0：输入够不够开诊？（§9 INVALID_INPUT）---------------
        missing = ctx.validate_minimum()
        if missing:
            state.terminal = TerminalState.INVALID_INPUT
            state.unresolved_reason = f"缺少必要输入：{', '.join(missing)}"
            return self._finalize(state)

        # ---- 步骤 1：轨迹分析（§12 第一问）-----------------------------
        state.trace = self.analyzer.analyze(ctx)
        state.trace_evidence = self._materialize_trace(state)
        state.notes.append(f"轨迹分析：{summarize_trace(state.trace)}")

        # ---- 步骤 2：故障分类（§4）-------------------------------------
        from ..classification.classifier import FailureClassifier

        classifier = FailureClassifier(self.llm)
        state.classification = classifier.classify(ctx, state.trace)
        state.notes.append(
            f"故障分类：{state.classification.primary_category.value} "
            f"（{state.classification.decided_by}，估计置信度 "
            f"{state.classification.confidence:.2f}）"
        )

        # ---- 步骤 3：展开候选假设（§5.1）-------------------------------
        state.manager = HypothesisManager.from_classification(state.classification)
        self._seed_from_experience(state, use_experience)
        self._apply_trace_hints(state)

        # ---- 步骤 4：诊断循环（§10.3）----------------------------------
        round_index = 0
        while state.terminal is None and round_index < self.budget.max_steps:
            result = self._diagnostic_round(state, round_index, use_templates)
            if result is not None:
                break
            round_index += 1

        if state.terminal is None:
            state.terminal = TerminalState.UNRESOLVED
            state.unresolved_reason = (
                f"达到最大诊断步数 {self.budget.max_steps}，"
                "仍没有充分证据指向单一根因"
            )

        # ---- 步骤 5：受控恢复 + 结果验证（§7）--------------------------
        if state.terminal is TerminalState.ROOT_CAUSE_IDENTIFIED:
            self._recovery_phase(state, auto_approve=auto_approve)

        return self._finalize(state)

    # ==================================================================
    # 单轮诊断
    # ==================================================================
    def _diagnostic_round(
        self,
        state: DiagnosisState,
        round_index: int,
        use_templates: bool,
    ) -> TerminalState | None:
        """跑一轮。返回非 ``None`` 表示循环应当结束。"""
        ctx = state.ctx
        manager = state.manager
        assert manager is not None

        # ---- 结论门：够不够下结论？-------------------------------------
        ready, reason = manager.conclusion_ready(self._source_of)
        state.notes.append(f"第 {round_index + 1} 轮结论门：{reason}")
        if ready:
            state.terminal = TerminalState.ROOT_CAUSE_IDENTIFIED
            return state.terminal

        # ---- 预算闸门（§10.3 第 6 条）----------------------------------
        exhausted = state.usage.exhausted(self.budget)
        if exhausted:
            state.terminal = TerminalState.UNRESOLVED
            state.unresolved_reason = f"预算耗尽（{exhausted}）；{reason}"
            return state.terminal

        # ---- 规划 ------------------------------------------------------
        plan = self._next_plan(state, round_index, use_templates)
        if plan is None or not plan.nodes:
            # 「没有更有区分力的检查可用」有两种截然相反的处境，必须分开处理：
            #
            #   领跑假设已经被主动推动过 -> 调查到头了，可以定论；
            #   领跑假设从未被主动推动过 -> 是查不动（不是查明白），只能 UNRESOLVED。
            #
            # 把这两种都归到 UNRESOLVED，会让系统在一锤定音之后卡住不下结论；
            # 都归到 ROOT_CAUSE_IDENTIFIED，则会让「什么都没查明」冒充「已定位」。
            leader = manager.leader()
            if (
                leader.posterior >= CONCLUSION.accept_probability
                and leader.discriminating_checks
            ):
                state.terminal = TerminalState.ROOT_CAUSE_IDENTIFIED
                state.notes.append(
                    "已无更有区分力的检查可做，且领跑假设 "
                    f"{leader.key} 有主动证据支持（后验 {leader.posterior:.2f}）-> 定论"
                )
                return state.terminal
            state.terminal = TerminalState.UNRESOLVED
            state.unresolved_reason = (
                "没有可执行的进一步检查：所有候选动作要么已经跑过，"
                "要么预期信息增益不足以说明问题；领跑假设也缺少主动证据支持"
            )
            return state.terminal

        # ---- 校验（§6.3 八项，确定性代码执行）--------------------------
        report = self.validator.validate(
            plan, ctx, budget=self.budget, usage=state.usage
        )
        state.validation_reports.append(report)
        if not report.ok:
            for issue in report.blocking:
                state.notes.append(
                    f"计划节点 {issue.node_id or '-'} 被拦下 [{issue.code}]：{issue.message}"
                )
            plan, _ = self.validator.sanitize(
                plan, ctx, budget=self.budget, usage=state.usage
            )
            if not plan.nodes:
                # 整份计划都不可执行 —— 退回贪心单节点，而不是就此放弃
                fallback = self.planner.build_dynamic(
                    ctx, manager, executed=state.executed_skills, max_nodes=1
                )
                if fallback is None or not fallback.nodes:
                    state.terminal = TerminalState.UNRESOLVED
                    state.unresolved_reason = "所有候选计划都未通过校验，且没有可回退的动作"
                    return state.terminal
                plan = fallback
                state.notes.append("整份计划不可执行，已回退到贪心单节点")

        # ---- 执行 ------------------------------------------------------
        state.plan_modes.append(plan.mode.value)
        state.plan_rationales.append(plan.rationale)
        outcome = self.executor.execute(
            plan, self.tool_ctx, state.usage, run_id=state.run_id
        )

        # ---- 观测 → 假设更新（§5.3 的「更新假设」）----------------------
        for node_result, observation in outcome.observations():
            spec = self.pool.get(node_result.skill)
            if spec is None or spec.kind != "diagnostic":
                continue
            if node_result.reused_from:
                state.notes.append(
                    f"复用 {node_result.skill} 的既有结果（{node_result.reused_from}，未调工具）"
                )
            report_item = manager.update(spec, observation, node_result.evidence_ids)
            state.diagnostic_path.append(node_result.skill)
            if not node_result.reused_from:
                state.executed_skills.add(node_result.skill)
            if report_item.moved:
                moved = ", ".join(
                    f"{key} {before:.2f}→{after:.2f}"
                    for key, before, after in report_item.moved
                )
                state.notes.append(
                    f"{node_result.skill} 观测={observation.value} 实际信息增益="
                    f"{report_item.information_gain:.3f}；{moved}"
                )
            else:
                state.notes.append(
                    f"{node_result.skill} 观测={observation.value}，未改变任何假设"
                    "（该检查在当前证据下没有区分力）"
                )

        for node_result in outcome.failed:
            state.notes.append(
                f"节点 {node_result.node_id}({node_result.skill}) 执行失败：{node_result.error}"
            )

        state.usage.steps += 1
        state.usage.elapsed_seconds += sum(
            item.duration_ms for item in outcome.results
        ) / 1000.0
        self._persist(state, plan, outcome)
        return None

    # ==================================================================
    # 恢复阶段
    # ==================================================================
    def _recovery_phase(self, state: DiagnosisState, *, auto_approve: bool) -> None:
        """诊断结束后的恢复阶段。

        进这个函数时 ``state.terminal`` 已经是 ``ROOT_CAUSE_IDENTIFIED``（「知道为什么」）。
        恢复阶段要做的是把它**升级**成 ``RESOLVED``（「知道为什么，且验证已经好了」），
        或者在条件不满足时原样退回。所以这里先把它清空，用一个干净的循环重新判定 ——
        直接沿用「非 None 即结束」会让循环体一次都不执行。
        """
        state.terminal = None
        ctx = state.ctx
        manager = state.manager
        assert manager is not None
        leader = manager.leader()

        while state.terminal is None:
            decision = self.recovery.evaluate(
                manager,
                attempted=state.attempted_recoveries,
                usage=state.usage,
            )
            state.recovery_decisions.append(decision)

            if decision.action is None:
                if decision.blocked_reason in (
                    "NO_AUTOMATED_RECOVERY", "LADDER_EXHAUSTED"
                ):
                    # 定位成功但无法自动恢复 —— 这**不是**失败，是边界
                    state.terminal = TerminalState.ROOT_CAUSE_IDENTIFIED
                    state.notes.append(f"恢复未执行：{decision.rationale}")
                else:
                    state.terminal = TerminalState.NEEDS_HUMAN
                    state.notes.append(f"恢复被拦下：{decision.rationale}")
                return

            if decision.requires_approval and not decision.approved:
                if auto_approve:
                    note = self.recovery.grant(decision.action)
                    state.notes.append(note)
                    continue        # 批准后重新过一遍验证门
                state.terminal = TerminalState.NEEDS_HUMAN
                state.notes.append(
                    f"需要人工审批：{decision.action}（{decision.tier.value}）—— {decision.rationale}"
                )
                return

            outcome = self.recovery.execute(
                decision,
                self.tool_ctx,
                state.usage,
                attempt=len(state.recoveries) + 1,
            )
            outcome = self.recovery.verify(
                outcome,
                self.tool_ctx,
                state.usage,
                root_cause_evidence=leader.supporting_evidence,
            )
            state.recoveries.append(outcome)
            state.attempted_recoveries.append(decision.action)
            state.diagnostic_path.append(f"recovery:{decision.action}")

            if outcome.executed and outcome.verified and outcome.rerun_passed:
                state.terminal = TerminalState.RESOLVED
                state.notes.append(
                    f"{decision.action} 后环境恢复且复测通过 -> RESOLVED"
                )
                return
            if not outcome.executed:
                state.terminal = TerminalState.NEEDS_HUMAN
                state.notes.append(f"恢复未执行：{outcome.note}")
                return
            state.notes.append(
                f"{decision.action} 未达效或复测未通过 -> 按阶梯升级（{outcome.note}）"
            )
            # 无效恢复动作记一笔失败经验（§8.2 的第三类）
            if not outcome.verified:
                case, created = self.experience.record_failure(
                    ctx,
                    attempted_actions=[decision.action],
                    symptom=f"{decision.action} 执行后环境状态未改变",
                )
                state.notes.append(
                    f"失败经验{'已入库' if created else '已存在（去重）'}：{case.case_id}"
                    f"（{decision.action} 对本故障无效）"
                )

        # 走完阶梯仍没解决
        if state.terminal is None:
            state.terminal = TerminalState.ROOT_CAUSE_IDENTIFIED
            state.unresolved_reason = "恢复阶梯已用尽，根因已定位但未解决"

    # ==================================================================
    # 规划
    # ==================================================================
    def _next_plan(
        self,
        state: DiagnosisState,
        round_index: int,
        use_templates: bool,
    ) -> DiagnosticPlan | None:
        manager = state.manager
        assert manager is not None and state.classification is not None

        mode, template = self.planner.select_mode(
            state.ctx,
            state.classification.primary_category,
            round_index=round_index,
            allow_templates=use_templates,
        )
        if template is not None:
            plan, _ = self.planner.build_from_template(state.ctx, template)
            return plan

        # 模式 C：先让 LLM 提一份候选 DAG（如果允许），校验不过再退回贪心
        if self.use_llm_planner:
            proposal = self.planner.build_from_llm(state.ctx, manager)
            if proposal is not None and proposal.nodes:
                report = self.validator.validate(
                    proposal, state.ctx, budget=self.budget, usage=state.usage
                )
                if report.ok:
                    state.notes.append(
                        f"采纳 LLM 提议的计划（{len(proposal.nodes)} 个节点，已通过校验）"
                    )
                    return proposal
                state.notes.append(
                    "LLM 提议的计划未通过校验，回退到信息增益贪心："
                    + "；".join(
                        f"[{issue.code}] {issue.message}" for issue in report.blocking
                    )
                )

        return self.planner.build_dynamic(
            state.ctx,
            manager,
            executed=state.executed_skills,
            max_nodes=self.budget.max_nodes_per_plan if self.planner.branching else 1,
        )

    # ==================================================================
    # 轨迹 → 证据 / 假设
    # ==================================================================
    def _materialize_trace(self, state: DiagnosisState) -> list[str]:
        """把轨迹分析的结果落成证据。

        这些是**观测事实**（节点确实自称成功、确实没有 output_ref），
        不是推断 —— 所以落 OBSERVED 而不是 INFERRED。
        """
        ctx = state.ctx
        trace = state.trace
        assert trace is not None
        index = {event.event_id: event for event in ctx.execution_trace}
        ids: list[str] = []

        if trace.phenomenon_node and trace.phenomenon_node in index:
            event = index[trace.phenomenon_node]
            evidence = self.store.record_raw(
                source="trace_analysis",
                claim=(
                    f"现象节点 {event.event_id}（{event.skill or event.agent}）报错 "
                    f"{event.error_code or event.status}"
                ),
                payload={"event_id": event.event_id, "skill": event.skill,
                         "error_code": event.error_code, "message": event.message},
                ref=f"artifact://trace/{event.event_id}",
            )
            ids.append(evidence.evidence_id)

        for anomaly in trace.anomalies:
            evidence = self.store.record_observed(
                source="trace_analysis",
                claim=(
                    f"{anomaly['event_id']}（{anomaly.get('skill')}）"
                    f" 属 {anomaly['kind']}：{'；'.join(anomaly['signals'])}"
                ),
                payload=anomaly,
                derived_from=ids[:1],
            )
            ids.append(evidence.evidence_id)
        return ids

    def _apply_trace_hints(self, state: DiagnosisState) -> None:
        """轨迹线索 → 调整先验（**不是**计成证据，见模块 docstring 末段）。"""
        manager = state.manager
        trace = state.trace
        assert manager is not None and trace is not None
        index = {event.event_id: event for event in state.ctx.execution_trace}

        for suspect_id in trace.suspect_nodes:
            event = index.get(suspect_id)
            if event is None:
                continue
            haystack = f"{event.skill} {event.tool or ''}".lower()
            for keyword, key in _SKILL_HYPOTHESIS_HINTS:
                if keyword in haystack:
                    manager.boost_prior(
                        key,
                        TRACE_PRIOR_BOOST,
                        f"轨迹里 {suspect_id} 自称成功但未通过校验",
                    )
                    state.notes.append(
                        f"轨迹线索：{suspect_id}（{event.skill}）疑似假成功 -> 提升 {key} 先验"
                    )
                    break

    # ==================================================================
    # 经验
    # ==================================================================
    def _seed_from_experience(self, state: DiagnosisState, enabled: bool) -> None:
        if not enabled or not self.experience.cases():
            return
        retrievals = self.experience.retrieve(state.ctx)
        state.notes.extend(
            f"经验检索：{item.case.case_id} 相似度 {item.score:.2f} "
            f"({'适用' if item.applicable else '不适用'}：{item.reason})"
            for item in retrievals
        )
        state.experience_notes = self.experience.seed_priors(state.manager, retrievals)
        state.notes.extend(state.experience_notes)

    # ==================================================================
    # 收尾
    # ==================================================================
    def _persist(self, state: DiagnosisState, plan: DiagnosticPlan, outcome) -> None:
        """把关键状态落检查点（§10.3 第 5 条）。"""
        self.executor.checkpoints.save(
            state.run_id,
            {
                "phase": "diagnosis",
                "terminal": state.terminal.value if state.terminal else None,
                "plan_id": plan.plan_id,
                "mode": plan.mode.value,
                "diagnostic_path": list(state.diagnostic_path),
                "usage": state.usage.as_dict(),
                "hypotheses": state.manager.summary() if state.manager else [],
                "executed": sorted(state.executed_skills),
            },
        )

    def _source_of(self, evidence_id: str) -> str | None:
        evidence = self.store.get(evidence_id)
        return evidence.source if evidence else None

    def _finalize(self, state: DiagnosisState) -> DiagnosisConclusion:
        manager = state.manager
        classification = state.classification
        terminal = state.terminal or TerminalState.UNRESOLVED

        root_cause: str | None = None
        root_cause_key: str | None = None
        confidence = 0.0
        evidence_ids: list[str] = list(state.trace_evidence)
        hypotheses: list[dict] = []

        if manager is not None:
            manager.finalize_statuses()
            hypotheses = manager.summary()
            leader = manager.leader()
            confidence = leader.posterior
            # NEEDS_HUMAN 也带上根因：人是被请来「批准一个已知动作」的，
            # 不是被请来「重新查一遍」的 —— 后者说明我们的诊断白做了。
            if terminal in (
                TerminalState.RESOLVED,
                TerminalState.ROOT_CAUSE_IDENTIFIED,
                TerminalState.NEEDS_HUMAN,
            ):
                root_cause_key = leader.key
                root_cause = leader.description
                evidence_ids = list(dict.fromkeys(
                    state.trace_evidence + leader.supporting_evidence
                ))

        # 结论里只引用**原始证据**，保证读者点下去看到的是日志/接口原文
        raw_evidence = self.store.raw_ancestors(evidence_ids)

        conclusion = DiagnosisConclusion(
            task_id=state.ctx.task_id,
            terminal_state=terminal,
            root_cause=root_cause,
            root_cause_key=root_cause_key,
            confidence=round(confidence, 3),
            evidence_ids=evidence_ids,
            classification=classification,
            trace_analysis=state.trace,
            hypotheses=hypotheses,
            diagnostic_path=state.diagnostic_path,
            recovery=state.recoveries,
            verification=self._verification_summary(state),
            unresolved_reason=state.unresolved_reason,
            plan_modes=state.plan_modes,
            budget=state.usage.as_dict(),
            metrics=self._metrics(state, raw_evidence),
            notes=list(state.notes),
        )

        # ---- 经验沉淀（§8.2 的质量门控流水线）---------------------------
        extraction = self.experience.extract(conclusion, state.ctx, self.store)
        conclusion.metrics["experience"] = {
            "decision": extraction.decision,
            "case_id": extraction.case.case_id if extraction.case else None,
            "reasons": extraction.reasons,
        }

        # 收尾再落一次检查点。只在中途落是不够的 —— 最后一步往往正是
        # 「读取终态」的那一步，如果进程在恢复验证的路上挂掉，磁盘上
        # 就只剩一个 terminal=None 的旧快照，等于把整段排障白做了。
        self.executor.checkpoints.save(
            state.run_id,
            {
                "phase": "final",
                "terminal": terminal.value,
                "root_cause": root_cause,
                "root_cause_key": root_cause_key,
                "confidence": conclusion.confidence,
                "diagnostic_path": list(state.diagnostic_path),
                "usage": state.usage.as_dict(),
                "hypotheses": hypotheses,
                "recovery": [
                    {"action": item.action, "verified": item.verified,
                     "rerun_passed": item.rerun_passed}
                    for item in state.recoveries
                ],
            },
        )
        return conclusion

    @staticmethod
    def _verification_summary(state: DiagnosisState) -> dict:
        if not state.recoveries:
            return {}
        last = state.recoveries[-1]
        return {
            "action": last.action,
            "tier": last.tier.value,
            "env_verified": last.verified,
            "rerun_passed": last.rerun_passed,
            "before": last.before_snapshot,
            "after": last.after_snapshot,
        }

    @staticmethod
    def _metrics(state: DiagnosisState, raw_evidence: list[str]) -> dict:
        """§9 那张指标表里本 Demo 能算出来的部分。"""
        notes = state.notes
        expected_ig = []
        actual_ig = []
        for note in notes:
            if "实际信息增益=" in note:
                try:
                    actual_ig.append(float(note.split("实际信息增益=")[1].split("；")[0]))
                except (IndexError, ValueError):
                    pass
        for note in state.plan_rationales:
            if "IG=" in note:
                for chunk in note.split("IG=")[1:]:
                    try:
                        expected_ig.append(float(chunk.split()[0]))
                    except (IndexError, ValueError):
                        pass
        return {
            "steps": state.usage.steps,
            "tool_calls": state.usage.tool_calls,
            "recovery_attempts": state.usage.recovery_attempts,
            "distinct_diagnostic_actions": len(state.executed_skills),
            "raw_evidence_anchors": len(raw_evidence),
            "expected_information_gain": [round(v, 3) for v in expected_ig],
            "realized_information_gain": [round(v, 3) for v in actual_ig],
            "plan_modes": state.plan_modes,
        }


def describe(conclusion: DiagnosisConclusion) -> str:
    """给终端用的一行摘要。"""
    if conclusion.root_cause_key:
        return (
            f"{conclusion.terminal_state.value} · 根因 {conclusion.root_cause_key}"
            f"（后验 {conclusion.confidence:.2f}）"
        )
    return conclusion.terminal_state.value


__all__ = [
    "TroubleshootingAgent",
    "DiagnosisState",
    "describe",
    "FailureCategory",
    "PlanMode",
    "ExperienceStatus",
    "Budget",
]
