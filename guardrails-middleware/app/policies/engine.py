"""Policy Engine（设计说明书 §14~§15）。

回答「这个风险应该怎么办」。输入阶段 + Findings，输出最高风险动作。
多个风险并存时按 Action Priority 取最保守动作（REDACT 不能覆盖 BLOCK）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.context import Stage
from ..core.decision import Action, PolicyDecision, resolve_actions
from .models import PolicyRule, PolicyTable

if TYPE_CHECKING:
    from ..core.finding import SecurityFinding


class PolicyEngine:
    def __init__(
        self,
        rules: PolicyTable | None = None,
        default_action: Action = Action.ALLOW,
    ) -> None:
        self.rules: PolicyTable = rules or {}
        self.default_action = default_action

    # ---- 查询 ---------------------------------------------------------------
    def rule_for(self, stage: "Stage | str", category: str) -> PolicyRule | None:
        stage_name = stage.name if isinstance(stage, Stage) else str(stage).upper()
        return self.rules.get(stage_name, {}).get(category)

    def action_for(self, stage: "Stage | str", category: str) -> Action:
        rule = self.rule_for(stage, category)
        return rule.action if rule else self.default_action

    # ---- 评估 ---------------------------------------------------------------
    def evaluate(self, stage: "Stage | str", findings: list["SecurityFinding"]) -> PolicyDecision:
        applied: list[tuple[str, str]] = []
        actions: list[Action] = []
        rule_params: dict[str, dict] = {}
        for finding in findings:
            rule = self.rule_for(stage, finding.category)
            action = rule.action if rule else self.default_action
            actions.append(action)
            applied.append((finding.category, action.value))
            if rule is not None and rule.params:
                rule_params[finding.category] = rule.params

        final = resolve_actions(actions)
        reason = "; ".join(f"{c}->{a}" for c, a in applied) or "no risk"
        return PolicyDecision(
            action=final, reason=reason, applied=applied, rules=rule_params
        )


__all__ = ["PolicyEngine"]
