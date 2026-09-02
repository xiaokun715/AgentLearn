"""ActionExecutor（设计说明书 §17）—— 统一处理五种 Action。

- ALLOW          放行
- BLOCK          抛 SecurityBlocked（Tool 永远不会执行）
- REDACT         内容脱敏后放行
- SANITIZE       中和注入指令后放行（Tool Result）
- RETRY          抛 RetryRequired（调用方带 guidance 重新生成）
- HUMAN_APPROVAL 创建审批票据并抛 ApprovalRequired

设计要点：REDACT / SANITIZE 都是**放行型内容变换**，当同一内容同时命中
PII/Secret（-> REDACT）与注入指令（-> SANITIZE）时**两者都要执行**——
不会因为 top Action 是 SANITIZE 而丢掉对手机号的脱敏（否则敏感信息会原样
放进 Context）。最终对外 Action 仍取最高优先级，实际执行的变换记录在
``result.metadata["transforms"]``。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.decision import Action, PolicyDecision
from ..core.exceptions import ApprovalRequired, RetryRequired, SecurityBlocked
from ..core.result import GuardrailResult
from .rewrite import Redactor, Sanitizer

if TYPE_CHECKING:
    from ..approval.service import ApprovalService
    from ..core.context import GuardrailContext
    from ..core.finding import SecurityFinding


class ActionExecutor:
    def __init__(
        self,
        redactor: Redactor | None = None,
        sanitizer: Sanitizer | None = None,
        approval_service: "ApprovalService | None" = None,
    ) -> None:
        self.redactor = redactor or Redactor()
        self.sanitizer = sanitizer or Sanitizer()
        self.approval_service = approval_service

    async def execute(
        self,
        context: "GuardrailContext",
        findings: list["SecurityFinding"],
        decision: PolicyDecision,
    ) -> GuardrailResult:
        action = decision.action

        if action == Action.ALLOW:
            return self._result(context, Action.ALLOW, findings, context.content,
                                message="allowed")

        if action == Action.BLOCK:
            raise SecurityBlocked(findings, context.stage.name, message=decision.reason)

        if action == Action.REDACT or action == Action.SANITIZE:
            return self._transform(context, findings, decision)

        if action == Action.RETRY:
            guidance = self._retry_guidance(findings, decision)
            raise RetryRequired(findings, context.stage.name, guidance=guidance)

        if action == Action.HUMAN_APPROVAL:
            req = None
            if self.approval_service is not None:
                req = self.approval_service.create(
                    request_id=context.request_id,
                    tenant_id=context.tenant_id,
                    agent=context.agent,
                    tool=context.tool_name or "",
                    arguments=context.tool_arguments or {},
                    risk_level=context.metadata.get("risk_level", "HIGH"),
                    reason=decision.reason,
                )
            raise ApprovalRequired(req, stage=context.stage.name)

        # 不可达：所有 Action 已覆盖
        raise ValueError(f"unsupported action: {action}")  # pragma: no cover

    # ---- 组合变换 ----------------------------------------------------------
    def _transform(
        self,
        context: "GuardrailContext",
        findings: list["SecurityFinding"],
        decision: PolicyDecision,
    ) -> GuardrailResult:
        # 每个 category 被映射成什么动作（applied 来自 PolicyEngine）
        action_by_category = {c: a for c, a in decision.applied}
        redact_findings = [
            f for f in findings if action_by_category.get(f.category) == Action.REDACT.value
        ]
        sanitize_findings = [
            f for f in findings if action_by_category.get(f.category) == Action.SANITIZE.value
        ]

        content = context.content
        transforms: list[str] = []
        parts: list[str] = []

        if sanitize_findings:
            content, changed = self.sanitizer.sanitize(content, sanitize_findings)
            if changed:
                transforms.append(Action.SANITIZE.value)
                parts.append("sanitized")
        if redact_findings:
            content, changed = self.redactor.redact(content, redact_findings)
            if changed:
                transforms.append(Action.REDACT.value)
                parts.append(f"redacted {len(redact_findings)} finding(s)")

        message = "no content changed" if not parts else ", ".join(parts)
        result = self._result(context, decision.action, findings, content, message=message)
        result.metadata["transforms"] = transforms
        return result

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _retry_guidance(
        findings: list["SecurityFinding"], decision: PolicyDecision
    ) -> str:
        lines = ["Your previous output does not satisfy the requirement. Please fix:"]
        for f in findings:
            lines.append(f"- {f.message}")
        max_retries = (decision.rules.get("SCHEMA_MISMATCH") or {}).get("max_retries")
        if max_retries is not None:
            lines.append(f"(allowed up to {max_retries} retries)")
        return "\n".join(lines)

    @staticmethod
    def _result(
        context: "GuardrailContext",
        action: Action,
        findings: list["SecurityFinding"],
        content,
        message: str,
    ) -> GuardrailResult:
        return GuardrailResult(
            stage=context.stage.name,
            action=action,
            request_id=context.request_id,
            findings=findings,
            content=content,
            message=message,
        )


__all__ = ["ActionExecutor"]
