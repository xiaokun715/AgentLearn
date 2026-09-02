"""Risk Policy（设计说明书 §18、§20 第三层）。

按 Tool 声明的 risk_level 映射到基础动作（默认 LOW/MEDIUM -> ALLOW，
HIGH/CRITICAL -> HUMAN_APPROVAL），``require_approval=True`` 强制人工放行。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.decision import Action
from .registry import ToolPolicy

DEFAULT_RISK_ACTIONS: dict[str, Action] = {
    "LOW": Action.ALLOW,
    "MEDIUM": Action.ALLOW,
    "HIGH": Action.HUMAN_APPROVAL,
    "CRITICAL": Action.HUMAN_APPROVAL,
}


@dataclass
class RiskPolicy:
    mappings: dict[str, Action] = field(default_factory=lambda: dict(DEFAULT_RISK_ACTIONS))

    def action_for_risk(self, risk_level: str) -> Action:
        return self.mappings.get(risk_level.upper(), Action.ALLOW)

    def decide(self, policy: ToolPolicy) -> tuple[Action, str]:
        """返回 (动作, 说明)。"""
        level = policy.risk_level.upper()
        base = self.action_for_risk(level)
        if base == Action.BLOCK:
            return Action.BLOCK, f"tool '{policy.name}' risk_level={level} is blocked by risk policy"

        if policy.require_approval or base == Action.HUMAN_APPROVAL:
            return Action.HUMAN_APPROVAL, (
                f"tool '{policy.name}' risk_level={level} requires human approval"
            )
        return Action.ALLOW, f"tool '{policy.name}' risk_level={level} allowed"


__all__ = ["RiskPolicy", "DEFAULT_RISK_ACTIONS"]
