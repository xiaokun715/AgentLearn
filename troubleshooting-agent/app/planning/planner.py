"""诊断规划器 —— 根据假设与证据生成下一步检查（说明书 §5.2 / §5.3 / §6.2）。

这是排障智能体与普通日志分析 Agent 最容易拉开差距的地方（说明书原话）。
规划器要回答的**不是**「哪个工具看起来最相关」，而是：

    哪个检查能最有效地**区分**当前这几个候选根因，且代价可接受？

所以选动作的分数是：

    Score(a) = IG(a) / (Cost(a) + λ·Risk(a) + ε)

其中 ``IG(a) = H(ℋ) - E_{o~a}[H(ℋ|o)]`` 由假设管理器算出。

三种 Workflow 生成模式按说明书 §6.2 实现，且**优先模板**：

- 模式 A ``PREDEFINED``：命中模板就按模板跑（稳定、可测试、成本可预期）；
- 模式 B ``TRIMMED``：模板里已有新鲜结果的节点改为复用，不重复执行；
- 模式 C ``DYNAMIC``：没有模板时，按信息增益贪心选动作，必要时组装并行分支。

模式 C 生成的 DAG 一律要过 :mod:`app.planning.validator`，**不接受「模型直接
生成一段可执行代码」**（§6.3 的第一条禁令）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..domain.enums import Observation, PlanMode
from ..domain.models import (
    ActionSpec,
    DiagnosticPlan,
    FailureContext,
    PlanNode,
)
from ..domain.policy import SCORING, WorkflowTemplate, template_for
from ..hypothesis.manager import HypothesisManager
from .actions import ActionPool


@dataclass
class ScoredAction:
    """候选动作及其评分明细 —— 明细要能展示，否则「为什么选它」无法复盘。"""

    spec: ActionSpec
    information_gain: float
    score: float

    def explain(self) -> str:
        return (
            f"{self.spec.name:<26} IG={self.information_gain:.3f} "
            f"cost={self.spec.cost:.1f} risk={self.spec.risk:.2f} "
            f"score={self.score:.3f}"
        )


#: 缓存结果的保质期。超过这个时长的「已有结果」不再可信 ——
#: 说明书 §6.2 模式 B：「需要检查已有结果的有效期、版本和上下文是否匹配，
#: 不能仅因为历史节点执行成功就直接复用。」
CACHE_TTL = timedelta(minutes=30)


def cache_valid(entry: dict, ctx: FailureContext) -> tuple[bool, str]:
    """判断一条缓存检查结果能否复用。"""
    if not entry:
        return False, "缓存为空"
    observation = entry.get("observation")
    if observation not in {item.value for item in Observation}:
        return False, "缓存里没有可用的观测结果"
    version = entry.get("software_version")
    if version and ctx.software_version and version != ctx.software_version:
        return False, f"软件版本已变化（缓存 {version} ≠ 当前 {ctx.software_version}）"
    collected_at = entry.get("collected_at")
    if collected_at:
        try:
            moment = datetime.fromisoformat(str(collected_at))
        except ValueError:
            return False, "缓存时间戳无法解析"
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ctx.observed_at.tzinfo)
        age = ctx.observed_at - moment
        if age > CACHE_TTL:
            return False, f"缓存已过期（{age.total_seconds() / 60:.1f} 分钟 > {CACHE_TTL.total_seconds() / 60:.0f} 分钟）"
    return True, "缓存有效"


class DiagnosticPlanner:
    """§6.2 三种模式的统一入口。"""

    def __init__(
        self,
        pool: ActionPool,
        *,
        permissions: set[str] | None = None,
        llm=None,
        branching: bool = False,
    ) -> None:
        self.pool = pool
        self.permissions = permissions or set()
        self.llm = llm
        self.branching = branching

    # ==================================================================
    # 评分与排序（§5.3）
    # ==================================================================
    def score(self, spec: ActionSpec, manager: HypothesisManager) -> ScoredAction:
        information_gain = manager.expected_information_gain(spec)
        denominator = spec.cost + SCORING.risk_weight * spec.risk + SCORING.epsilon
        return ScoredAction(
            spec=spec,
            information_gain=information_gain,
            score=information_gain / denominator if denominator > 0 else 0.0,
        )

    def rank(self, manager: HypothesisManager, *, exclude: set[str] | None = None) -> list[ScoredAction]:
        """按分数排序所有候选诊断动作。

        ``exclude`` 用来剔除**本轮已经执行过**的动作 —— 同一个检查连跑两次
        不会带来新信息（观测已经确定了），但会白白花掉一次预算。
        """
        exclude = exclude or set()
        keys = {h.key for h in manager.active()}
        ranked = [
            self.score(spec, manager)
            for spec in self.pool.relevant_to(keys)
            if spec.name not in exclude
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked

    def best(self, manager: HypothesisManager, *, exclude: set[str] | None = None) -> ScoredAction | None:
        ranked = self.rank(manager, exclude=exclude)
        if not ranked:
            return None
        top = ranked[0]
        if top.information_gain < SCORING.min_information_gain:
            return None
        return top

    # ==================================================================
    # 模式选择
    # ==================================================================
    def select_mode(
        self,
        ctx: FailureContext,
        classification_category,
        *,
        round_index: int,
        allow_templates: bool = True,
    ) -> tuple[PlanMode, WorkflowTemplate | None]:
        """优先模板，模板不适用才动态生成（§6.2 的最后一条建议）。

        模板只在**第一轮**使用：它的价值是「常见故障一步到位」；
        如果第一轮跑完还没定性，说明这属于「模板没覆盖到的情况」，
        后面继续按模板跑只会重复烧预算 —— 交给模式 C 逐轮自适应。
        """
        if allow_templates and round_index == 0:
            template = template_for(classification_category)
            if template is not None:
                return PlanMode.PREDEFINED, template
        return PlanMode.DYNAMIC, None

    # ==================================================================
    # 模式 A / B：模板与裁剪
    # ==================================================================
    def build_from_template(
        self,
        ctx: FailureContext,
        template: WorkflowTemplate,
    ) -> tuple[DiagnosticPlan, PlanMode]:
        """模式 A + 模式 B 一起产出：能复用的节点标 ``reused_from``。

        「裁剪」不是把节点删掉，而是把它变成一次**复用**：
        节点仍然在 DAG 里（依赖关系不变），但执行器不会真的调工具，
        而是把缓存里的观测结果重新计入证据流。
        """
        nodes: list[PlanNode] = []
        reused: list[str] = []
        skipped_reason: list[str] = []

        for node_id, skill, depends_on, cache_key in template.nodes:
            reused_from: str | None = None
            if cache_key:
                entry = ctx.cached_checks.get(cache_key)
                ok, reason = cache_valid(entry, ctx)
                if ok:
                    reused_from = f"cache:{cache_key}"
                    reused.append(f"{skill}（{reason}）")
                elif entry:
                    skipped_reason.append(f"{skill}：{reason}")
            nodes.append(
                PlanNode(
                    id=node_id,
                    skill=skill,
                    depends_on=list(depends_on),
                    inputs={"cache_key": cache_key} if cache_key else {},
                    reused_from=reused_from,
                )
            )

        mode = PlanMode.TRIMMED if reused else PlanMode.PREDEFINED
        plan = DiagnosticPlan(
            mode=mode,
            nodes=nodes,
            template_id=template.template_id,
            rationale=(
                f"命中模板 {template.template_id}：{template.description}"
                + (f"；复用 {len(reused)} 个已有结果（{'; '.join(reused)}）" if reused else "")
                + (f"；不可复用 {len(skipped_reason)} 个（{'; '.join(skipped_reason)}）" if skipped_reason else "")
            ),
        )
        return plan, mode

    # ==================================================================
    # 模式 C：动态生成
    # ==================================================================
    def build_dynamic(
        self,
        ctx: FailureContext,
        manager: HypothesisManager,
        *,
        executed: set[str] | None = None,
        max_nodes: int | None = None,
    ) -> DiagnosticPlan | None:
        """按信息增益贪心生成候选 DAG。

        ``branching=True`` 时会额外挂上**独立且仍有区分力**的第二个节点：
        两个节点没有依赖、也不争抢同一个互斥资源（例如环境），才可以并行。
        说明书 §6.2 的原话是「并行节点只在它们的输入已经具备、
        没有互斥资源需求时才能同时执行」。
        """
        executed = executed or set()
        ranked = self.rank(manager, exclude=executed)
        if not ranked:
            return None

        top = ranked[0]
        if top.information_gain < SCORING.min_information_gain:
            return None

        nodes = [
            PlanNode(
                id="N1",
                skill=top.spec.name,
                depends_on=[],
                inputs={"resource": top.spec.resource},
            )
        ]
        rationale = [f"贪心选择 {top.spec.name}（{top.explain()}）"]

        if self.branching and (max_nodes is None or max_nodes > 1):
            for candidate in ranked[1:]:
                # 只有「独立且不共享互斥资源」的动作才允许并行
                if candidate.information_gain < top.information_gain * 0.4:
                    break
                if candidate.spec.resource and candidate.spec.resource == top.spec.resource:
                    rationale.append(
                        f"跳过 {candidate.spec.name}：与 {top.spec.name} 争抢资源 "
                        f"{candidate.spec.resource}，不能并行"
                    )
                    continue
                if not candidate.spec.idempotent and candidate.spec.side_effect == "mutate":
                    continue
                nodes.append(
                    PlanNode(
                        id=f"N{len(nodes) + 1}",
                        skill=candidate.spec.name,
                        depends_on=[],
                        inputs={"resource": candidate.spec.resource},
                    )
                )
                rationale.append(
                    f"并行挂载 {candidate.spec.name}（{candidate.explain()}）"
                )
                if max_nodes is not None and len(nodes) >= max_nodes:
                    break

        return DiagnosticPlan(
            mode=PlanMode.DYNAMIC,
            nodes=nodes,
            rationale="；".join(rationale),
        )

    # ==================================================================
    # LLM 提议的 DAG（§6.3：模型只输出结构化节点计划）
    # ==================================================================
    def build_from_llm(
        self,
        ctx: FailureContext,
        manager: HypothesisManager,
    ) -> DiagnosticPlan | None:
        """让 LLM 提一份节点计划 —— 但它只是一份**候选**，必须过校验器。

        模型输出的形状只有 ``nodes[].{id, skill, depends_on}``，
        没有代码、没有 shell、没有表达式。即便这样，它仍然可能写出
        环、未知 skill、越权动作 —— 所以校验器不是可选项（见 workflow 场景）。
        """
        if self.llm is None:
            return None
        proposal = self.llm.propose_plan(ctx, manager)
        if not proposal:
            return None
        try:
            plan = DiagnosticPlan.model_validate(proposal)
        except Exception:
            return None
        plan.proposed_by = "llm"
        return plan

    # ==================================================================
    # 恢复动作选择（§7.1）
    # ==================================================================
    # 这里**故意没有** ``select_recovery()``。
    #
    # 恢复动作的选取不是「按分数排序」的规划问题，而是「按证据匹配 + 风险递增」
    # 的授权问题 —— 它需要看权限集、审批记录、恢复次数预算、前后快照，
    # 全部在 :class:`~app.recovery.controller.RecoveryController` 里。
    #
    # 早先版本在规划器里也写了一份「挑恢复动作」的排序，结果是同一件事有两处实现：
    # 规划器按 ``risk`` 排序，控制器按 ``RECOVERY_LADDER`` 排序，两者对
    # 软件缺陷场景给出的答案不一样。删掉它是为了**只留一个真相来源**。
