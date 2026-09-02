"""Action 与优先级（设计说明书 §8、§15）。

多个 Finding 同时命中时不能互相覆盖（例如 REDACT 不能覆盖 BLOCK），
因此定义 Action Priority：BLOCK > HUMAN_APPROVAL > RETRY > SANITIZE > REDACT > ALLOW，
最终取最高风险动作。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Action(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REDACT = "redact"
    SANITIZE = "sanitize"            # 仅内容变换，可继续放行（TOOL_RESULT）
    RETRY = "retry"
    HUMAN_APPROVAL = "human_approval"

    def __str__(self) -> str:
        return self.value


# 优先级越高越保守（值越大 = 越不能放行）
ACTION_PRIORITY: dict[Action, int] = {
    Action.ALLOW: 0,
    Action.REDACT: 1,
    Action.SANITIZE: 2,
    Action.RETRY: 3,
    Action.HUMAN_APPROVAL: 4,
    Action.BLOCK: 5,
}

# 放行型动作：动作之后内容可以继续进入后续流程
PASS_THROUGH_ACTIONS = {Action.ALLOW, Action.REDACT, Action.SANITIZE}


def resolve_actions(actions: list[Action]) -> Action:
    """取最高优先级动作（§15：不能 REDACT 覆盖 BLOCK）。"""
    if not actions:
        return Action.ALLOW
    return max(actions, key=lambda a: ACTION_PRIORITY[a])


@dataclass
class PolicyDecision:
    """Policy Engine 的产出：最高风险动作 + 命中的 (category, action) 明细。

    ``rules`` 携带每个命中 category 对应的规则参数（如 retry 的 max_retries），
    供 ActionExecutor 消费（例如 RETRY 次数上限，见 §27）。
    """

    action: Action
    reason: str = ""
    applied: list[tuple[str, str]] = field(default_factory=list)  # (category, action.value)
    rules: dict[str, dict] = field(default_factory=dict)          # category -> rule.params

    @property
    def allowed(self) -> bool:
        return self.action in PASS_THROUGH_ACTIONS

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "applied": [{"category": c, "action": a} for c, a in self.applied],
        }


__all__ = ["Action", "ACTION_PRIORITY", "PASS_THROUGH_ACTIONS", "resolve_actions", "PolicyDecision"]
