"""对外返回的结果类型。

- ``GuardrailResult``  内容阶段（INPUT / CONTEXT / TOOL_RESULT / OUTPUT）的结果
- ``ToolCheckResult``  TOOL_CALL 阶段 Tool Guardrail 的结果（§31）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.decision import Action, PASS_THROUGH_ACTIONS
from .finding import SecurityFinding


@dataclass
class GuardrailResult:
    """内容安全检查结果（SDK / API 统一返回）。"""

    stage: str
    action: Action
    request_id: str = ""
    findings: list[SecurityFinding] = field(default_factory=list)
    content: Any = None                    # 变换后的内容；BLOCK / RETRY 时为 None
    message: str = ""
    retry_guidance: str | None = None      # RETRY 时给 LLM 的重试说明（§27）
    approval_id: str | None = None         # HUMAN_APPROVAL 时创建的票据 id
    metadata: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        """该动作之后内容是否可以继续放行。"""
        return self.action in PASS_THROUGH_ACTIONS

    @property
    def blocked(self) -> bool:
        return self.action == Action.BLOCK

    @property
    def action_value(self) -> str:
        return self.action.value

    def to_dict(self, include_content: bool = True) -> dict:
        d: dict[str, Any] = {
            "request_id": self.request_id,
            "stage": self.stage,
            "action": self.action.value,
            "blocked": self.blocked,
            "allowed": self.allowed,
            "findings": [f.to_dict() for f in self.findings],
        }
        if include_content:
            d["content"] = self.content if not isinstance(self.content, str) else self.content
        if self.message:
            d["message"] = self.message
        if self.retry_guidance:
            d["retry_guidance"] = self.retry_guidance
        if self.approval_id:
            d["approval_id"] = self.approval_id
        return d


@dataclass
class ToolCheckResult:
    """Tool Call 安全检查结果（§31 / §20 三层 allowlist）。"""

    agent: str
    tool: str
    action: Action
    request_id: str = ""
    risk_level: str = "UNKNOWN"
    reason: str = ""
    arguments: dict | None = None
    approval_id: str | None = None
    findings: list[SecurityFinding] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.action in PASS_THROUGH_ACTIONS

    @property
    def blocked(self) -> bool:
        return self.action == Action.BLOCK

    @property
    def needs_approval(self) -> bool:
        return self.action == Action.HUMAN_APPROVAL

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "request_id": self.request_id,
            "agent": self.agent,
            "tool": self.tool,
            "action": self.action.value,
            "risk": self.risk_level,
            "reason": self.reason,
            "blocked": self.blocked,
            "needs_approval": self.needs_approval,
            "findings": [f.to_dict() for f in self.findings],
        }
        if self.approval_id:
            d["approval_id"] = self.approval_id
        return d


__all__ = ["GuardrailResult", "ToolCheckResult"]
