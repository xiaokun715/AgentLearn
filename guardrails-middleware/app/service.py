"""Guardrails —— 面向业务 Agent 的门面 / SDK（设计说明书 §33~§34）。

业务 Agent 不关心内部有哪些 Detector；只需要调用：

    g = Guardrails(...)
    r = await g.check_input(query)          # 用户输入
    r = await g.check_tool_result(res)      # Tool 返回（间接注入防线）
    c = await g.check_output(text)          # 最终输出
    c = await g.check_tool(agent, tool, args)  # Tool 调用前检查

SDK 内部自动完成 Detect -> Policy -> Action -> Audit -> Metrics。
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from .actions.executor import ActionExecutor
from .actions.rewrite import Redactor, Sanitizer
from .approval.service import ApprovalService
from .audit.repository import AuditRepository
from .config import GuardrailsConfig
from .core.context import GuardrailContext, Stage
from .core.decision import Action
from .core.exceptions import ApprovalRequired, RetryRequired, SecurityBlocked
from .core.pipeline import GuardrailPipeline
from .core.result import GuardrailResult, ToolCheckResult
from .detectors.base import Detector
from .metrics import Metrics
from .policies.engine import PolicyEngine
from .tools.registry import ToolRegistry
from .tools.risk import RiskPolicy
from .validators.argument import ArgumentValidator
from .validators.tool import ToolCallValidator

logger = logging.getLogger(__name__)

_STAGE_LABELS = {Stage.INPUT: "INPUT", Stage.CONTEXT: "CONTEXT",
                 Stage.TOOL_RESULT: "TOOL_RESULT", Stage.OUTPUT: "OUTPUT"}


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


class Guardrails:
    """组合全部安全组件，对外暴露 Check 方法（Agent SDK 形态，§33）。"""

    def __init__(
        self,
        *,
        config: GuardrailsConfig | None = None,
        detectors: list[Detector] | None = None,
        policy_engine: PolicyEngine | None = None,
        redactor: Redactor | None = None,
        audit: AuditRepository | None = None,
        metrics: Metrics | None = None,
        approval_service: ApprovalService | None = None,
        tool_registry: ToolRegistry | None = None,
        risk_policy: RiskPolicy | None = None,
    ) -> None:
        self.config = config or GuardrailsConfig()
        self.metrics = metrics or Metrics(latency_window=self.config.latency_window)
        self.audit = audit or AuditRepository(max_events=self.config.audit_max_events)
        self.approval_service = approval_service or ApprovalService(
            ttl_seconds=self.config.approval_ttl_seconds
        )

        # ---- 组件装配（Detector -> Policy -> Action）------------------------
        self.policy_engine = policy_engine or PolicyEngine()
        self.redactor = redactor or Redactor()
        executor = ActionExecutor(
            redactor=self.redactor,
            sanitizer=Sanitizer(),
            approval_service=self.approval_service,
        )
        self._detectors = detectors or []
        self._pipeline = GuardrailPipeline(
            self._detectors, self.policy_engine, executor, audit=self.audit
        )

        # ---- Tool 安全 ------------------------------------------------------
        self.tool_registry = tool_registry or ToolRegistry()
        self.risk_policy = risk_policy or RiskPolicy()
        self.argument_validator = ArgumentValidator()
        self.tool_validator = ToolCallValidator(
            self.tool_registry, self.argument_validator, self.risk_policy,
            self.approval_service,
        )

    # ====================================================================
    # 内容阶段守卫：INPUT / CONTEXT / TOOL_RESULT / OUTPUT（§34）
    # ====================================================================
    async def check_input(
        self,
        content: Any,
        *,
        agent: str = "default",
        user_id: str = "anonymous",
        metadata: dict | None = None,
        request_id: str | None = None,
    ) -> GuardrailResult:
        return await self._guard_content(
            Stage.INPUT, content, agent=agent, user_id=user_id,
            metadata=metadata, request_id=request_id,
        )

    async def check_context(
        self,
        content: Any,
        *,
        agent: str = "default",
        user_id: str = "anonymous",
        metadata: dict | None = None,
        request_id: str | None = None,
    ) -> GuardrailResult:
        """外部内容进入 Context 前的安全过滤（Context Assembly 之前，§24）。"""
        return await self._guard_content(
            Stage.CONTEXT, content, agent=agent, user_id=user_id,
            metadata=metadata, request_id=request_id,
        )

    async def check_tool_result(
        self,
        content: Any,
        *,
        tool_name: str | None = None,
        agent: str = "default",
        user_id: str = "anonymous",
        metadata: dict | None = None,
        request_id: str | None = None,
    ) -> GuardrailResult:
        """Tool Result Guardrail —— Indirect Prompt Injection 的关键防线（§22）。"""
        return await self._guard_content(
            Stage.TOOL_RESULT, content, agent=agent, user_id=user_id,
            tool_name=tool_name, metadata=metadata, request_id=request_id,
        )

    async def check_output(
        self,
        content: Any,
        *,
        agent: str = "default",
        user_id: str = "anonymous",
        schema: dict | None = None,
        metadata: dict | None = None,
        request_id: str | None = None,
    ) -> GuardrailResult:
        """Output Guardrail（§25~§27）。``schema`` 提供时做 JSON Schema 校验（→ RETRY）。"""
        meta = dict(metadata or {})
        if schema is not None:
            meta["schema"] = schema
        return await self._guard_content(
            Stage.OUTPUT, content, agent=agent, user_id=user_id,
            metadata=meta, request_id=request_id,
        )

    async def _guard_content(
        self,
        stage: Stage,
        content: Any,
        *,
        agent: str,
        user_id: str,
        tool_name: str | None = None,
        metadata: dict | None = None,
        request_id: str | None = None,
    ) -> GuardrailResult:
        rid = request_id or new_request_id()
        ctx = GuardrailContext(
            request_id=rid,
            tenant_id=self.config.tenant_id,
            user_id=user_id,
            agent=agent,
            stage=stage,
            content=content,
            metadata=metadata or {},
            tool_name=tool_name,
        )
        label = _STAGE_LABELS.get(stage, stage.name)
        self.metrics.inc("guardrail_requests_total", {"stage": label})
        t0 = time.perf_counter()
        try:
            result = await self._pipeline.process(ctx)
            # ALLOW / REDACT / SANITIZE 正常返回 GuardrailResult
        except SecurityBlocked as exc:
            result = GuardrailResult(
                stage=stage.name, action=Action.BLOCK, request_id=rid,
                findings=exc.findings, content=None, message=str(exc) or exc.stage,
            )
        except RetryRequired as exc:
            result = GuardrailResult(
                stage=stage.name, action=Action.RETRY, request_id=rid,
                findings=exc.findings, content=None,
                message="output retry required", retry_guidance=exc.guidance,
            )
        except ApprovalRequired as exc:  # 内容阶段一般不触发，兜底
            result = GuardrailResult(
                stage=stage.name, action=Action.HUMAN_APPROVAL, request_id=rid,
                findings=[], content=None,
                approval_id=getattr(exc.request, "id", None),
                message=str(exc),
            )
        self.metrics.observe("guardrail_latency", time.perf_counter() - t0, {"stage": label})
        self._metric_action(result)
        self._metric_findings(result.findings)
        return result

    # ====================================================================
    # Tool 阶段守卫：TOOL_CALL（§19~§21）
    # ====================================================================
    async def check_tool(
        self,
        agent: str,
        tool: str,
        arguments: dict | None,
        *,
        user_id: str = "anonymous",
        metadata: dict | None = None,
        request_id: str | None = None,
    ) -> ToolCheckResult:
        rid = request_id or new_request_id()
        result = await self.tool_validator.check(
            agent, tool, arguments,
            request_id=rid, tenant_id=self.config.tenant_id, user_id=user_id,
        )
        # 审计（Tool 路径不走 Detector，单独记录决策事件）
        ctx = GuardrailContext(
            request_id=rid,
            tenant_id=self.config.tenant_id,
            user_id=user_id,
            agent=agent,
            stage=Stage.TOOL_CALL,
            content=None,
            metadata=metadata or {},
            tool_name=tool,
            tool_arguments=arguments or {},
        )
        self.audit.record_decision(
            context=ctx, detector="tool", category="TOOL_CALL",
            action=result.action.value,
            metadata={
                "tool": tool,
                "risk": result.risk_level,
                "reason": result.reason,
                "approval_id": result.approval_id,
            },
        )
        label = "TOOL_CALL"
        self.metrics.inc("guardrail_requests_total", {"stage": label})
        action = result.action.value
        if action in ("block", "redact", "sanitize", "retry", "human_approval", "allow"):
            self.metrics.inc(f"guardrail_{action}_total", {"stage": label})
        return result

    # ====================================================================
    # Human Approval（§28）
    # ====================================================================
    def list_approvals(self, status: str | None = None) -> list[dict]:
        return [r.to_dict() for r in self.approval_service.list(status)]

    def get_approval(self, approval_id: str) -> dict:
        return self.approval_service.get(approval_id).to_dict()

    def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str = "human",
        note: str = "",
    ) -> dict:
        return self.approval_service.decide(
            approval_id, approved=approved, decided_by=decided_by, note=note
        ).to_dict()

    # ====================================================================
    # 只读查询
    # ====================================================================
    def list_audit_events(self, limit: int = 50, **filters: str | None) -> list[dict]:
        return [e.to_dict() for e in self.audit.list(limit=limit, **filters)]

    def list_tools(self) -> list[dict]:
        out = []
        for name in self.tool_registry.names():
            p = self.tool_registry.get(name)
            out.append({
                "name": p.name,
                "risk_level": p.risk_level,
                "require_approval": p.require_approval,
                "allowed_agents": p.allowed_agents,
                "description": p.description,
            })
        return out

    def policy_report(self) -> dict:
        """当前策略的只读视图（便于 Demo / 排障）。"""
        rules = {}
        for stage, cat_map in self.policy_engine.rules.items():
            rules[stage] = {c: r.action.value for c, r in cat_map.items()}
        return {"stages": rules, "default": self.policy_engine.default_action.value}

    # ---- metrics helpers --------------------------------------------------
    def _metric_action(self, result) -> None:
        a = result.action.value
        if a in ("block", "redact", "sanitize", "retry", "human_approval", "allow"):
            self.metrics.inc(f"guardrail_{a}_total", {"stage": getattr(result, "stage", "")})

    def _metric_findings(self, findings) -> None:
        seen: dict[str, int] = {}
        for f in findings:
            seen[f.category] = seen.get(f.category, 0) + 1
        for category, count in seen.items():
            self.metrics.inc("guardrail_detection_total", {"category": category}, value=count)


__all__ = ["Guardrails", "new_request_id"]
