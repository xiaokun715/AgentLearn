"""调查主循环（说明书 §4）。

    while 有预算 and 证据不足:
        tool = planner.select(活跃假设, 已查工具)      ← 动态：每一步重新算
        verdict = gateway.check(tool)                 ← 硬边界：权限/范围/预算
        if not verdict.allowed: 记一笔，继续
        result = execute(tool)                        ← 唯一能碰到数据的地方
        update_hypotheses(result)                     ← 证据 → 假设权重
        if 结论门通过: break                           ← 确定性判据，不是模型自评
    生成报告（含处置**建议**）

三件事值得单独说：

1. **工具调用全部经过网关**（§3.2）。Agent 想调 ``apply_disposition``
   会被挡下并记进审计 —— 这条边界不是靠提示词。
2. **证据 → 假设的更新是确定性的规则**（§4.4）：支持就加权重、反驳就减。
   模型只在两个地方出现：把一堆证据归纳成一段话，以及在方式二里提出模式假设。
3. **结论由代码判定**（§4.5 ``ConclusionPolicy``）：证据来源数、是否数据缺失、
   是否陷入无新信息的空转。**不能问模型「你觉得够了吗」**。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import (
    Disposition,
    EvidenceKind,
    HypothesisStatus,
    InvestigationOutcome,
    Observation,
    RiskLevel,
)
from ..domain.models import (
    Alert,
    Evidence,
    Hypothesis,
    InvestigationReport,
    ToolCall,
    ToolResult,
)
from ..domain.policy import (
    BUDGET,
    CONCLUSION,
    HYPOTHESIS_LIBRARY,
    InvestigationBudget,
)
from ..gateway.tool_gateway import ToolGateway, build_scope
from ..sim import RiskWorld
from ..tools import investigation as tool_impl
from .planner import InvestigationPlanner

#: 证据对假设权重的推动幅度
SUPPORT_DELTA = 0.25
REFUTE_DELTA = 0.20


@dataclass
class InvestigationState:
    alert: Alert
    hypotheses: dict[str, Hypothesis] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    executed: set[str] = field(default_factory=set)
    #: 拿到过**有效观测**的工具（INCONCLUSIVE / 被拒的不算）——
    #: 前置条件必须看它，否则「设备图挂了」之后图分析还会被当成可执行
    succeeded: set[str] = field(default_factory=set)
    path: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tool_calls: int = 0
    cost: float = 0.0
    elapsed_ms: int = 0
    barren_rounds: int = 0

    def add_evidence(self, evidence: Evidence) -> None:
        self.evidence.append(evidence)

    @property
    def evidence_ids(self) -> list[str]:
        return [item.evidence_id for item in self.evidence]

    def sources_for(self, key: str) -> set[str]:
        """某个假设的支持证据来自哪些工具 —— 结论门要数**独立来源**。"""
        hypothesis = self.hypotheses[key]
        index = {item.evidence_id: item for item in self.evidence}
        return {index[eid].source for eid in hypothesis.support if eid in index}


class Investigator:
    """一次告警调查的完整过程。"""

    def __init__(
        self,
        world: RiskWorld,
        gateway: ToolGateway,
        planner: InvestigationPlanner,
        *,
        budget: InvestigationBudget = BUDGET,
        llm=None,
    ) -> None:
        self.world = world
        self.gateway = gateway
        self.planner = planner
        self.budget = budget
        self.llm = llm

    # ==================================================================
    def investigate(self, alert: Alert) -> tuple[InvestigationReport, InvestigationState]:
        state = self._init_state(alert)
        scope = build_scope(self.world, alert.entity_id)
        entity_device = self._device_of(alert.entity_id)

        while True:
            if self._budget_exhausted(state):
                state.notes.append(f"预算耗尽：{self._budget_reason(state)}")
                break

            scored = self.planner.select(
                state.hypotheses, state.executed, satisfied=state.succeeded,
                cost_penalty=0.12 if state.tool_calls else 0.0,
            )
            if scored is None:
                state.notes.append("没有可执行的进一步检查了")
                break

            progressed = self._run_tool(scored.spec.name, state, scope, entity_device,
                                        alert)
            if not progressed:
                state.barren_rounds += 1
                if state.barren_rounds >= self.budget.max_barren_rounds:
                    state.notes.append(
                        f"连续 {state.barren_rounds} 轮没有取得新信息，停止调查"
                    )
                    break
            else:
                state.barren_rounds = 0

            ready, reason = self._conclusion_ready(state)
            if ready:
                state.notes.append(f"结论门通过：{reason}")
                break

        report = self._build_report(state)
        return report, state

    # ==================================================================
    def _init_state(self, alert: Alert) -> InvestigationState:
        hypotheses = {
            key: Hypothesis(key=key, description=spec.description)
            for key, spec in HYPOTHESIS_LIBRARY.items()
        }
        uniform = 1.0 / len(hypotheses)
        for item in hypotheses.values():
            item.weight = uniform
        return InvestigationState(alert=alert, hypotheses=hypotheses)

    # ==================================================================
    def _run_tool(
        self,
        name: str,
        state: InvestigationState,
        scope: set[str],
        entity_device: str,
        alert: Alert,
    ) -> bool:
        arguments = self._arguments_for(name, alert, entity_device)
        call = ToolCall(tool=name, arguments=arguments, reason="风险调查")

        verdict = self.gateway.check(
            call, scope=scope, used_budget=state.cost, cost_limit=self.budget.max_cost
        )
        state.tool_calls += 1

        if not verdict.allowed:
            # 被拦下不是异常，是**设计**。记一笔继续，不要重试 ——
            # 重试一个权限不足的调用，只会在审计里刷屏。
            state.denied.append(f"{name}({verdict.deny_reason.value if verdict.deny_reason else '?'})")
            state.notes.append(f"工具 {name} 被网关拦下：{verdict.message}")
            state.executed.add(name)      # 不再重试同一个被拒的工具
            # 注意**不加入 succeeded**：被拒 ≠ 查过了，依赖它的工具也不该放行
            return False

        handler = tool_impl.resolve(name)
        result: ToolResult = handler(self.world, arguments)
        state.executed.add(name)
        state.path.append(name)
        spec = self.gateway.specs[name]
        state.cost += spec.cost
        state.elapsed_ms += spec.latency_ms
        if result.observation not in (Observation.INCONCLUSIVE, Observation.ERROR):
            state.succeeded.add(name)

        for evidence in result.evidence:
            state.add_evidence(evidence)

        changed = self._apply_observation(state, name, result)
        state.notes.append(
            f"{name} → {result.observation.value}：{result.summary[:60]}"
        )
        return changed

    # ==================================================================
    @staticmethod
    def _arguments_for(name: str, alert: Alert, entity_device: str) -> dict:
        context = alert.context
        if name == "query_device_graph" or name == "run_graph_analysis":
            return {"device_id": entity_device}
        if name == "query_ip_cluster":
            return {"ip": str(context.get("ip", ""))}
        if name == "search_similar_cases":
            return {"keyword": ""}
        if name == "query_risk_model_detail":
            return {
                "model_score": alert.model_score,
                "top_features": list(context.get("model_features", [])),
            }
        if name in ("query_account_profile", "query_transaction_history",
                    "query_coupon_usage"):
            return {"user_id": alert.entity_id}
        return {"entity_id": alert.entity_id, "alert_id": alert.alert_id}

    def _device_of(self, user_id: str) -> str:
        for device_id, device in self.world.devices.items():
            if user_id in device.accounts:
                return device_id
        return ""

    # ==================================================================
    # 证据 → 假设（§4.4）
    # ==================================================================
    def _apply_observation(
        self, state: InvestigationState, tool: str, result: ToolResult
    ) -> bool:
        """把一次观测转成假设权重的变化。返回「是否取得了新信息」。

        **确定性规则，不看模型脸色。** 映射表写在下面：

        ==================== ==========================================
        观测                 含义
        ==================== ==========================================
        ANOMALY              针对的假设被支持
        NORMAL               针对的假设被反驳
        INCONCLUSIVE         不更新任何假设（**但会支持「数据不足」假设**）
        ERROR                同 INCONCLUSIVE
        ==================== ==========================================

        ``INCONCLUSIVE`` 那一行是本节最容易被做错的地方：
        把「查不到」当成「没问题」，会让系统在数据源故障时安静地放行 ——
        而这类故障在真实系统里并不罕见。
        """
        spec = self.gateway.specs.get(tool)
        evidence_map = dict(spec.evidence_map) if spec else {}
        evidence_ids = [item.evidence_id for item in result.evidence]

        if result.observation in (Observation.INCONCLUSIVE, Observation.ERROR):
            insufficient = state.hypotheses.get("H_INSUFFICIENT_DATA")
            if insufficient is not None:
                insufficient.support.extend(evidence_ids)
                insufficient.checked_by.append(tool)
                self._reweigh(state)
            # **返回 False**：这次调用没有给任何一个风险假设带来信息。
            # 把它算成「有进展」会让空转计数永远不增长 ——
            # 于是数据源全部故障时，Agent 会一路查到底，最后产出一份
            # 看起来查了很多、实际什么都没查到的报告。
            return False

        if not evidence_map:
            return False

        changed = False
        for key, polarity in evidence_map.items():
            hypothesis = state.hypotheses.get(key)
            if hypothesis is None:
                continue
            hypothesis.checked_by.append(tool)
            # ``+`` 表示「异常即支持」，``-`` 表示「异常即反驳」
            supports = (result.observation is Observation.ANOMALY) == (polarity == "+")
            if supports:
                hypothesis.support.extend(evidence_ids)
                hypothesis.weight = min(0.95, hypothesis.weight + SUPPORT_DELTA)
            else:
                hypothesis.refute.extend(evidence_ids)
                hypothesis.weight = max(0.02, hypothesis.weight - REFUTE_DELTA)
            changed = True
        self._reweigh(state)
        return changed

    @staticmethod
    def _reweigh(state: InvestigationState) -> None:
        total = sum(item.weight for item in state.hypotheses.values()) or 1.0
        for item in state.hypotheses.values():
            item.weight = round(item.weight / total, 4)

        for item in state.hypotheses.values():
            supports, refutes = len(item.support), len(item.refute)
            # **证据冲突时不下单边判断。** 早先版本只判断「有没有反驳证据」，
            # 于是「3 条支持 + 1 条反驳」会被判成 REFUTED —— 一条弱反证
            # 推翻了三条强支持，最后把一个明确的误报报成了「转人工」。
            if refutes >= CONCLUSION.refute_threshold and refutes > supports:
                item.status = HypothesisStatus.REFUTED
            elif supports >= 2 and refutes == 0:
                item.status = HypothesisStatus.SUPPORTED
            elif supports or refutes:
                item.status = HypothesisStatus.UNDECIDED
            else:
                item.status = HypothesisStatus.PENDING

    # ==================================================================
    # 结论门（§4.5）—— **确定性判据，不问模型**
    # ==================================================================
    def _conclusion_ready(self, state: InvestigationState) -> tuple[bool, str]:
        risk_keys = ["H_BULK_REGISTER", "H_SHARED_DEVICE", "H_REFUND_ABUSE"]
        supported_risk = [
            key for key in risk_keys
            if state.hypotheses[key].status is HypothesisStatus.SUPPORTED
            and len(state.sources_for(key)) >= CONCLUSION.min_supporting_sources
        ]
        if supported_risk:
            return True, (f"假设 {supported_risk} 已被 "
                          f"{CONCLUSION.min_supporting_sources} 个独立来源支持")

        legit = state.hypotheses["H_LEGITIMATE"]
        # **判据是「所有风险假设都被反驳」，不是「没有风险假设成立」。**
        # 后者会被「还没查」蒙混过关：如果 REFUND_ABUSE 从没被检验过，
        # 它既不是 SUPPORTED 也不是 REFUTED —— 用它去证伪风险，
        # 等于把「没查」当成「没问题」。这是调查类系统最容易犯的假阴性。
        all_risk_refuted = all(
            state.hypotheses[key].status is HypothesisStatus.REFUTED for key in risk_keys
        )
        if (legit.status is HypothesisStatus.SUPPORTED
                and len(state.sources_for("H_LEGITIMATE")) >= CONCLUSION.min_supporting_sources
                and all_risk_refuted):
            return True, "正常行为假设被多个独立来源支持，且全部风险假设均已被证据反驳"

        insufficient = state.hypotheses["H_INSUFFICIENT_DATA"]
        if (insufficient.status is HypothesisStatus.SUPPORTED
                and len(state.sources_for("H_INSUFFICIENT_DATA"))
                >= CONCLUSION.min_supporting_sources):
            return True, "关键数据源不可用，无法得出可辩护的结论"

        return False, "证据尚不充分"

    # ==================================================================
    def _budget_exhausted(self, state: InvestigationState) -> bool:
        return bool(self._budget_reason(state))

    def _budget_reason(self, state: InvestigationState) -> str:
        if state.tool_calls >= self.budget.max_tool_calls:
            return f"工具调用达到上限 {self.budget.max_tool_calls}"
        if state.cost > self.budget.max_cost:
            return f"成本达到上限 {self.budget.max_cost}"
        if state.elapsed_ms >= self.budget.max_elapsed_ms:
            return f"耗时达到上限 {self.budget.max_elapsed_ms}ms"
        return ""

    # ==================================================================
    # 报告（§4.6）
    # ==================================================================
    def _build_report(self, state: InvestigationState) -> InvestigationReport:
        outcome, disposition, confidence, summary = self._conclude(state)
        missing = self._missing_evidence(state)
        return InvestigationReport(
            alert_id=state.alert.alert_id,
            entity_id=state.alert.entity_id,
            outcome=outcome,
            disposition=disposition,
            executed=False,        # **永远 False**：Agent 没有处置权
            summary=summary,
            hypotheses=[
                {
                    "key": item.key,
                    "description": item.description,
                    "status": item.status.value,
                    "weight": item.weight,
                    "support": len(item.support),
                    "refute": len(item.refute),
                    "sources": sorted(state.sources_for(item.key)),
                }
                for item in sorted(state.hypotheses.values(), key=lambda i: -i.weight)
            ],
            evidence_ids=state.evidence_ids,
            investigation_path=list(state.path),
            missing_evidence=missing,
            confidence=confidence,
            tool_calls=state.tool_calls,
            elapsed_ms=state.elapsed_ms,
            denied_calls=list(state.denied),
            notes=list(state.notes),
        )

    def _conclude(
        self, state: InvestigationState
    ) -> tuple[InvestigationOutcome, Disposition, float, str]:
        risk_keys = ["H_BULK_REGISTER", "H_SHARED_DEVICE", "H_REFUND_ABUSE"]
        supported = [
            key for key in risk_keys
            if state.hypotheses[key].status is HypothesisStatus.SUPPORTED
        ]
        legit = state.hypotheses["H_LEGITIMATE"]
        insufficient = state.hypotheses["H_INSUFFICIENT_DATA"]

        # ---- 风险假设成立 ------------------------------------------------
        if supported:
            sources = set()
            for key in supported:
                sources |= state.sources_for(key)
            if len(sources) >= CONCLUSION.min_supporting_sources:
                names = "、".join(HYPOTHESIS_LIBRARY[key].description for key in supported)
                return (
                    InvestigationOutcome.CONFIRMED_RISK,
                    Disposition.KEEP_BLOCK,
                    min(0.95, 0.55 + 0.1 * len(sources)),
                    f"证据支持：{names}；共 {len(sources)} 个独立来源",
                )
            return (
                InvestigationOutcome.INSUFFICIENT_EVIDENCE,
                Disposition.ESCALATE,
                0.45,
                f"发现 {supported} 的迹象，但只有 "
                f"{len(sources)} 个独立来源，不足以确认",
            )

        # ---- 数据缺失 ----------------------------------------------------
        if insufficient.status is HypothesisStatus.SUPPORTED:
            return (
                InvestigationOutcome.INSUFFICIENT_EVIDENCE,
                Disposition.ESCALATE,
                0.3,
                "关键数据源不可用或返回不完整，无法给出可辩护的结论，转人工",
            )

        # ---- 正常行为 ----------------------------------------------------
        #
        # **这里必须和 `_conclusion_ready` 用同一条判据。**
        # 早先版本里 `_conclusion_ready` 要求「所有风险假设都被反驳」，
        # 而这里只看「正常假设被支持」—— 于是循环因为别的原因提前退出时，
        # 报告会给出一个**比循环本身的判定更宽松**的结论：
        # 一个从没查过的维度被当成了「没问题」。
        # 两处判据不一致，是这类系统最难发现的一类 bug。
        all_risk_refuted = all(
            state.hypotheses[key].status is HypothesisStatus.REFUTED for key in risk_keys
        )
        if legit.status is HypothesisStatus.SUPPORTED and all_risk_refuted:
            return (
                InvestigationOutcome.LIKELY_FALSE_POSITIVE,
                Disposition.CLOSE_AS_FALSE_POSITIVE,
                min(0.9, 0.5 + 0.1 * len(state.sources_for("H_LEGITIMATE"))),
                "多个独立来源指向正常行为；风险假设均已被反驳",
            )

        # ---- 有维度根本没被检验 -> 证据不足（而不是「没风险」）----------
        # **这是本模块最重要的一条兜底。** 把「没查」归成「没问题」，
        # 是调查类系统最典型的假阴性 —— 而且它安静、可解释、看起来很正常。
        unchecked = [
            key for key in risk_keys
            if state.hypotheses[key].status in (HypothesisStatus.PENDING,
                                                HypothesisStatus.UNDECIDED)
        ]
        if unchecked:
            return (
                InvestigationOutcome.INSUFFICIENT_EVIDENCE,
                Disposition.ESCALATE,
                0.35,
                f"以下维度没有被有效检验：{unchecked} —— "
                "在没有覆盖完整之前不能判定「无风险」",
            )

        # ---- 证据自相矛盾 ------------------------------------------------
        return (
            InvestigationOutcome.NEEDS_HUMAN_JUDGEMENT,
            Disposition.ESCALATE,
            0.4,
            "证据同时支持风险与正常两种解释，涉及业务判断，转人工",
        )

    @staticmethod
    def _missing_evidence(state: InvestigationState) -> list[str]:
        """还缺什么证据 —— 报告里必须写清楚（§4.6）。

        一份只说结论不说缺口的报告，会让人以为「已经查透了」。
        """
        missing: list[str] = []
        for name, spec in state.hypotheses.items():
            if spec.status in (HypothesisStatus.PENDING, HypothesisStatus.UNDECIDED):
                missing.append(f"{name}：尚未被任何工具有效检验")
        return missing


__all__ = ["Investigator", "InvestigationState", "RiskLevel", "EvidenceKind"]
