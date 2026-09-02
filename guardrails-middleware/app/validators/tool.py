"""ToolCallValidator —— Tool 调用前检查链（设计说明书 §19~§21）。

执行顺序（§19）：
    Tool Allowlist -> Agent Permission -> Risk Policy
            -> Argument Schema -> Resource Policy -> Action
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.decision import Action
from ..core.result import ToolCheckResult

if TYPE_CHECKING:
    from ..approval.service import ApprovalService
    from ..tools.registry import ToolRegistry
    from ..tools.risk import RiskPolicy
    from .argument import ArgumentValidator


class ToolCallValidator:
    def __init__(
        self,
        registry: "ToolRegistry",
        argument_validator: "ArgumentValidator",
        risk_policy: "RiskPolicy",
        approval_service: "ApprovalService | None" = None,
    ) -> None:
        self.registry = registry
        self.argument_validator = argument_validator
        self.risk_policy = risk_policy
        self.approval_service = approval_service

    async def check(
        self,
        agent: str,
        tool: str,
        arguments: dict | None,
        *,
        request_id: str = "",
        tenant_id: str = "tenant_001",
        user_id: str = "",
    ) -> ToolCheckResult:
        policy = self.registry.get(tool)

        # 第一层：Tool Allowlist（§20）
        if policy is None:
            return ToolCheckResult(
                agent=agent, tool=tool, action=Action.BLOCK,
                request_id=request_id, risk_level="UNKNOWN",
                reason=f"tool '{tool}' is not in the allowlist",
                arguments=arguments,
            )

        # 第二层：Agent Permission（§20）
        from ..tools.permission import check_agent_permission

        ok, reason = check_agent_permission(agent, policy)
        if not ok:
            return ToolCheckResult(
                agent=agent, tool=tool, action=Action.BLOCK,
                request_id=request_id, risk_level=policy.risk_level,
                reason=reason, arguments=arguments,
            )

        # 第三层：Argument Schema + Resource Policy（§21）
        issue = self.argument_validator.first_error(arguments, policy)
        if issue is not None:
            return ToolCheckResult(
                agent=agent, tool=tool, action=Action.BLOCK,
                request_id=request_id, risk_level=policy.risk_level,
                reason=f"invalid arguments: {issue}", arguments=arguments,
            )

        # Resource Policy：规范化后校验资源边界（防 /tmp/../etc/passwd 穿越）
        from ..tools.resource import check_boundary

        boundary_issues = check_boundary(arguments or {}, policy.resource_boundary)
        if boundary_issues:
            return ToolCheckResult(
                agent=agent, tool=tool, action=Action.BLOCK,
                request_id=request_id, risk_level=policy.risk_level,
                reason="invalid arguments: " + "; ".join(boundary_issues),
                arguments=arguments,
            )

        # 第四层：Risk Policy（§18）
        action, reason = self.risk_policy.decide(policy)
        approval_id = None
        if action == Action.HUMAN_APPROVAL and self.approval_service is not None:
            req = self.approval_service.create(
                request_id=request_id or tool,
                tenant_id=tenant_id,
                agent=agent,
                tool=tool,
                arguments=arguments or {},
                risk_level=policy.risk_level,
                reason=reason,
            )
            approval_id = req.id

        return ToolCheckResult(
            agent=agent, tool=tool, action=action,
            request_id=request_id, risk_level=policy.risk_level,
            reason=reason, arguments=arguments, approval_id=approval_id,
        )


__all__ = ["ToolCallValidator"]
