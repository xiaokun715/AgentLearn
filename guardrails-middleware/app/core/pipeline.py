"""Security Pipeline —— 项目核心执行链（设计说明书 §16）。

    Request -> Normalize -> Detect -> Aggregate Findings -> Evaluate Policy
            -> Execute Action -> Audit -> Return

Pipeline 完全不关心是 PII / Injection / Secret，也不关心 Block / Redact / Retry，
所有逻辑都来自插件化模块（Detector / PolicyEngine / ActionExecutor / Audit）。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..core.decision import Action, PolicyDecision, resolve_actions
from ..core.exceptions import SecurityBlocked

if TYPE_CHECKING:
    from .context import GuardrailContext
    from .finding import SecurityFinding

logger = logging.getLogger(__name__)


class GuardrailPipeline:
    def __init__(
        self,
        detectors: list,
        policy_engine,
        action_executor,
        audit=None,
    ) -> None:
        self.detectors = detectors
        self.policy_engine = policy_engine
        self.action_executor = action_executor
        self.audit = audit

    async def process(self, context: "GuardrailContext") -> "object":
        """完整走一遍执行链，返回 ActionExecutor 的结果（或抛出领域异常）。"""
        # ---- 1. Detect ----------------------------------------------------
        findings: list["SecurityFinding"] = []
        for detector in self.detectors:
            if not detector.applicable(context.stage):
                continue
            try:
                result = await detector.detect(context)
                findings.extend(result or [])
            except Exception as exc:  # noqa: BLE001
                # 安全边界必须 fail-closed：某个 Detector 出错时不能让内容「未检测」放行。
                # 转成一个 CRITICAL DETECTOR_ERROR Finding 并整体 BLOCK。
                logger.exception("detector %s failed at stage=%s (fail-closed)",
                                 detector.name, context.stage)
                from ..core.finding import SecurityFinding

                error_finding = SecurityFinding(
                    detector=detector.name,
                    category="DETECTOR_ERROR",
                    severity="CRITICAL",
                    confidence=1.0,
                    message=f"detector {detector.name} failed: {exc}",
                )
                raise SecurityBlocked(
                    [error_finding], context.stage.name,
                    message=f"detector {detector.name} failed (fail-closed)",
                ) from exc

        # ---- 2. Aggregate + Evaluate Policy -------------------------------
        decision: PolicyDecision = self.policy_engine.evaluate(context.stage, findings)
        logger.debug(
            "stage=%s findings=%d decision=%s",
            context.stage.name, len(findings), decision.action.value,
        )

        # ---- 3. Audit（先审计再执行，保证 BLOCK 也被记录） -----------------
        if self.audit is not None:
            for finding in findings:
                self.audit.record_from_finding(
                    context=context,
                    finding=finding,
                    action=self.policy_engine.action_for(context.stage, finding.category),
                    resolved=decision.action,
                )

        # ---- 4. Execute Action -------------------------------------------
        return await self.action_executor.execute(context, findings, decision)


__all__ = ["GuardrailPipeline"]
