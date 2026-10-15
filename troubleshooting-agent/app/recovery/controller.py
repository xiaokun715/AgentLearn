"""受控恢复控制器 —— 诊断与恢复之间的那道闸门（说明书 §7）。

说明书开篇就把这条边界划得很硬：

> **诊断阶段可以收集证据、检查状态；恢复阶段则可能修改设备配置、重启服务、
> 重新申请环境或重新执行测试。**
> 因此，不能让排障 Agent 只要认为「可能是环境问题」，就立即重启设备或修改参数。

本模块把「恢复」拆成三件不同的事，各自有独立的失败模式：

1. :meth:`RecoveryController.evaluate` —— **验证门**：这次恢复该不该做、谁来批；
2. :meth:`RecoveryController.execute`  —— **执行**：真的改世界，并留下前后快照；
3. :meth:`RecoveryController.verify`   —— **结果验证**：改完之后世界真的好了吗。

其中第 3 步有个必须说清的边界（说明书 §7.2 原话）：
**复测通过不能单独证明原始根因已经确定**，它只能证明「当前环境下复测成功」。
所以 :class:`~app.domain.models.RecoveryOutcome` 里 ``verified``（环境恢复正常）
与 ``rerun_passed``（复测通过）是两个字段，不是一个布尔值。
"""
from __future__ import annotations

from ..clock import Clock
from ..domain.enums import Observation, RiskTier
from ..domain.models import (
    Budget,
    BudgetUsage,
    PlanNode,
    RecoveryDecision,
    RecoveryOutcome,
)
from ..domain.policy import APPROVAL_GRANTS, APPROVAL_MATRIX
from ..evidence.store import EvidenceStore
from ..hypothesis.manager import HypothesisManager
from ..planning.actions import ActionPool
from ..tools.base import ToolContext, ToolError, ToolRegistry

#: 恢复升级阶梯：从风险最低的动作开始试，无效再升级。
#:
#: 这是本模块最核心的一条策略 —— **不要一上来就重启设备**。
#: 很多所谓的「环境异常」其实是观测缓存过期，R1 刷新一次就好了；
#: 只有刷新确实无效，才说明世界真的坏了，这时才值得动用 R2。
RECOVERY_LADDER: dict[str, list[str]] = {
    "H_ENV": ["refresh_environment_state", "reallocate_environment", "restart_device"],
    "H_TOOL": ["refresh_environment_state", "reallocate_environment"],
    "H_CONFIG": [],       # 配置修正要走变更流程，不在自动恢复范围内
    "H_CASE": [],         # 改测试预期 = R4，默认禁止自动执行
    "H_SOFTWARE": ["create_defect_report"],
}

#: 哪些恢复动作「达效」之后值得复测。非环境类恢复（建单、改配置）复测没有意义。
_VERIFY_BY_RERUN = {
    "refresh_environment_state",
    "reallocate_environment",
    "restart_device",
}


class RecoveryController:
    """恢复动作的验证门 + 执行 + 结果验证。"""

    def __init__(
        self,
        pool: ActionPool,
        registry: ToolRegistry,
        store: EvidenceStore,
        clock: Clock,
        *,
        permissions: set[str] | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.pool = pool
        self.registry = registry
        self.store = store
        self.clock = clock
        self.permissions = permissions if permissions is not None else set()
        #: 人工审批后临时授予的权限（§7.2 授权策略）
        self.granted_permissions: set[str] = set()
        self.approved_actions: set[str] = set()
        self.budget = budget or Budget()

    # ==================================================================
    # 审批
    # ==================================================================
    def grant(self, action: str, *, reviewer: str = "ops-lead") -> str:
        """模拟人工审批通过：授予该动作所需权限。

        注意 R4 **不在**这个通道里 —— :data:`~app.domain.policy.APPROVAL_GRANTS`
        里没有 R4 动作的映射，所以「批了也不能跑」是结构性的，不是纪律性的。
        """
        permission = APPROVAL_GRANTS.get(action)
        if permission is None:
            raise PermissionError(
                f"{action} 不在可审批授予的范围内（R4 动作默认禁止自动执行）"
            )
        self.granted_permissions.add(permission)
        self.approved_actions.add(action)
        return f"已由 {reviewer} 批准 {action}，临时授予权限 {permission}"

    def is_approved(self, action: str) -> bool:
        return action in self.approved_actions

    # ==================================================================
    # 1. 验证门（§7.2）
    # ==================================================================
    def evaluate(
        self,
        manager: HypothesisManager,
        *,
        attempted: list[str] | None = None,
        usage: BudgetUsage | None = None,
    ) -> RecoveryDecision:
        """回答三个问题：有没有客观证据？动作匹配吗？允许自动执行吗？"""
        attempted = attempted or []
        usage = usage or BudgetUsage()
        leader = manager.leader()

        # ---- 门 1：异常必须有客观证据 ---------------------------------
        evidence_ids = (
            leader.supporting_evidence
            or [eid for item in manager.all() for eid in item.supporting_evidence]
        )
        if not leader.supporting_evidence:
            return RecoveryDecision(
                action=None,
                matched_evidence=[],
                rationale=(
                    f"领跑假设 {leader.key} 没有任何支持证据 —— "
                    "没有客观证据就不动世界（§7.2 第一道门）"
                ),
                blocked_reason="NO_EVIDENCE",
            )

        # ---- 门 2：恢复动作必须与证据指向的原因匹配 ---------------------
        ladder = RECOVERY_LADDER.get(leader.key, [])
        if not ladder:
            return RecoveryDecision(
                action=None,
                matched_evidence=evidence_ids,
                rationale=(
                    f"{leader.key} 没有对应的自动恢复动作 —— "
                    "配置修正要走变更流程、改测试预期属于 R4，都不在自动通道内"
                ),
                blocked_reason="NO_AUTOMATED_RECOVERY",
            )
        remaining = [name for name in ladder if name not in attempted]
        if not remaining:
            return RecoveryDecision(
                action=None,
                matched_evidence=evidence_ids,
                rationale=(
                    f"针对 {leader.key} 的恢复阶梯已用尽（已尝试 {attempted or '无'}）—— "
                    "不无限重试（§7.2）"
                ),
                blocked_reason="LADDER_EXHAUSTED",
            )

        action = remaining[0]
        spec = self.pool.get(action)
        if spec is None:
            return RecoveryDecision(
                action=None,
                matched_evidence=evidence_ids,
                rationale=f"恢复动作 {action} 不在动作池里",
                blocked_reason="UNKNOWN_ACTION",
            )

        # ---- 门 3：风险分级 + 权限 + 预算 -------------------------------
        decision = RecoveryDecision(
            action=action,
            tier=spec.tier,
            matched_evidence=evidence_ids,
            rationale=(
                f"{leader.key} 后验 {leader.posterior:.2f}，证据 {len(leader.supporting_evidence)} 条；"
                f"按阶梯选择风险最低的 {action}（{spec.tier.value}，审批策略 "
                f"{APPROVAL_MATRIX.get(spec.tier, '?')}）"
            ),
        )

        if spec.tier.forbidden:
            decision.blocked_reason = "FORBIDDEN_R4"
            decision.rationale += "；R4 默认禁止自动执行"
            return decision

        if usage.recovery_attempts >= self.budget.max_recovery_attempts:
            decision.blocked_reason = "RECOVERY_BUDGET_EXHAUSTED"
            decision.rationale += (
                f"；恢复次数已达上限 {self.budget.max_recovery_attempts}"
            )
            return decision

        if spec.tier.needs_approval:
            decision.requires_approval = True
            if self.is_approved(action):
                decision.approved = True
                decision.rationale += "；已获人工批准"
            else:
                decision.blocked_reason = "NEEDS_APPROVAL"
                decision.rationale += "；R3 需要人工审批，当前未获批准"
                return decision
        elif spec.tier.auto_executable:
            decision.approved = True
        else:
            # R2：检查权限与配额（§7.1「检查配额、成本和幂等性」）
            if spec.permission and spec.permission not in self.permissions | self.granted_permissions:
                decision.blocked_reason = "PERMISSION_DENIED"
                decision.rationale += f"；缺少权限 {spec.permission}"
                return decision
            decision.approved = True
            decision.rationale += "；R2 已通过配额与幂等性检查"

        return decision

    # ==================================================================
    # 2. 执行
    # ==================================================================
    def execute(
        self,
        decision: RecoveryDecision,
        tool_ctx: ToolContext,
        usage: BudgetUsage,
        *,
        attempt: int = 1,
    ) -> RecoveryOutcome:
        if not decision.approved or not decision.action:
            return RecoveryOutcome(
                attempt=attempt,
                action=decision.action or "(none)",
                tier=decision.tier,
                executed=False,
                note=f"未执行：{decision.blocked_reason}",
            )

        spec = self.pool.require(decision.action)
        node = PlanNode(id=f"R{attempt}", skill=decision.action, inputs={})
        before = tool_ctx.platform.snapshot()

        try:
            observation = self.registry.resolve(decision.action)(tool_ctx, node)
        except ToolError as exc:
            return RecoveryOutcome(
                attempt=attempt, action=decision.action, tier=spec.tier,
                executed=True, before_snapshot=before, note=f"恢复动作失败：{exc}",
            )

        usage.tool_calls += 1
        usage.recovery_attempts += 1
        self.store.materialize(observation.evidence, source=decision.action)
        after = tool_ctx.platform.snapshot()

        return RecoveryOutcome(
            attempt=attempt,
            action=decision.action,
            tier=spec.tier,
            executed=True,
            before_snapshot=before,
            after_snapshot=after,
            verified=observation.outcome is Observation.NORMAL,
            note=observation.summary,
        )

    # ==================================================================
    # 3. 结果验证（§7.2）
    # ==================================================================
    def verify(
        self,
        outcome: RecoveryOutcome,
        tool_ctx: ToolContext,
        usage: BudgetUsage,
        *,
        root_cause_evidence: list[str],
    ) -> RecoveryOutcome:
        """验证恢复是否达效，并（在达效时）执行复测。

        这里要守住说明书 §7.2 的那句话：复测通过 **≠** 根因已确定。
        所以我们记两条证据：一条是「环境恢复正常」的观测，一条是「复测通过」
        的验证结论 —— 后者必须指回前者与根因证据，形成可追溯的链条。
        """
        if not outcome.executed:
            return outcome

        if not outcome.verified:
            outcome.note += "；恢复未达效，将按阶梯升级到下一个动作"
            return outcome

        if outcome.action not in _VERIFY_BY_RERUN:
            # 不是环境类恢复（例如建缺陷单），复测没有意义
            outcome.note += "；非环境类恢复，跳过复测"
            return outcome

        node = PlanNode(id=f"V{outcome.attempt}", skill="rerun_test", inputs={})
        observation = self.registry.resolve("rerun_test")(tool_ctx, node)
        usage.tool_calls += 1
        evidence = self.store.materialize(observation.evidence, source="rerun_test")
        usage.recovery_attempts += 1

        rerun_passed = observation.outcome is Observation.NORMAL
        outcome.rerun_passed = rerun_passed

        self.store.verify(
            source="recovery_controller",
            claim=(
                f"恢复动作 {outcome.action} 后复测{'通过' if rerun_passed else '仍然失败'}"
            ),
            derived_from=[eid for eid in root_cause_evidence if eid]
            + [item.evidence_id for item in evidence],
            payload={
                "action": outcome.action,
                "tier": outcome.tier.value,
                "before": outcome.before_snapshot,
                "after": outcome.after_snapshot,
                "rerun": observation.summary,
            },
        )
        outcome.note += (
            "；复测通过（注意：这证明的是当前环境下问题不再复现，"
            "根因结论仍需结合恢复前后的差异）"
            if rerun_passed else "；复测仍然失败"
        )
        return outcome

    def ladder_for(self, hypothesis_key: str) -> list[str]:
        return list(RECOVERY_LADDER.get(hypothesis_key, []))

    @staticmethod
    def risk_note(tier: RiskTier) -> str:
        return f"{tier.value} -> {APPROVAL_MATRIX.get(tier, '?')}"
