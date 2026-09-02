"""领域异常（设计说明书 §17、§28）。

- ``SecurityBlocked``    -> Action.BLOCK，调用方应终止该次执行
- ``RetryRequired``      -> Action.RETRY，调用方应带 guidance 重试（如 Schema 不符）
- ``ApprovalRequired``   -> Action.HUMAN_APPROVAL，调用方应先获得人工放行
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context import GuardrailContext
    from .decision import Action
    from .finding import SecurityFinding


class GuardrailsError(Exception):
    """Guardrails 领域异常基类。"""


class ConfigError(GuardrailsError):
    """策略 / 配置加载错误（YAML 缺失、结构非法）。"""


class SecurityBlocked(GuardrailsError):
    """风险被 BLOCK：内容或 Tool Call 被拒绝，绝不放行。"""

    def __init__(
        self,
        findings: list["SecurityFinding"],
        stage: str,
        message: str = "Blocked by guardrails",
    ) -> None:
        self.findings = findings
        self.stage = stage
        super().__init__(message)


class RetryRequired(GuardrailsError):
    """需要调用方重试（典型：OUTPUT 不符合 JSON Schema，§26~§27）。"""

    def __init__(
        self,
        findings: list["SecurityFinding"],
        stage: str,
        guidance: str,
        message: str = "Retry required",
    ) -> None:
        self.findings = findings
        self.stage = stage
        self.guidance = guidance
        super().__init__(message)


class ApprovalRequired(GuardrailsError):
    """高风险动作需要人工审批（§28）。携带已创建的 ApprovalRequest。"""

    def __init__(self, request: "object", stage: str = "TOOL_CALL") -> None:
        self.request = request
        self.stage = stage
        super().__init__(
            f"tool '{getattr(request, 'tool', '?')}' requires human approval "
            f"(request_id={getattr(request, 'id', '?')})"
        )


class NotFoundError(GuardrailsError):
    """ApprovalRequest / 资源不存在。"""


class InvalidApprovalError(GuardrailsError):
    """审批状态非法（重复审批 / 已过期 / 已结束）。"""


__all__ = [
    "GuardrailsError",
    "ConfigError",
    "SecurityBlocked",
    "RetryRequired",
    "ApprovalRequired",
    "NotFoundError",
    "InvalidApprovalError",
]
