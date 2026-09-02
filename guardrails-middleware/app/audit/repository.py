"""Audit 存储（设计说明书 §29）。

Demo 用内存环形缓冲；接口隔离，生产可替换 SQLite / ClickHouse。
"""
from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING, Any

from .event import SecurityEvent

if TYPE_CHECKING:
    from ..core.context import GuardrailContext
    from ..core.decision import Action
    from ..core.finding import SecurityFinding


class AuditRepository:
    def __init__(self, max_events: int = 5000) -> None:
        self._events: deque[SecurityEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    # ---- 写 ---------------------------------------------------------------
    def append(self, event: SecurityEvent) -> SecurityEvent:
        with self._lock:
            self._events.appendleft(event)
        return event

    def record_from_finding(
        self,
        context: "GuardrailContext",
        finding: "SecurityFinding",
        action: "Action",
        resolved: "Action",
    ) -> SecurityEvent:
        meta: dict[str, Any] = {"resolved": resolved.value}
        if context.tool_name:
            meta["tool"] = context.tool_name
        return self.append(
            SecurityEvent(
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                agent=context.agent,
                stage=context.stage.name,
                detector=finding.detector,
                category=finding.category,
                severity=finding.severity,
                action=action.value,
                metadata=meta,
            )
        )

    def record_decision(
        self,
        *,
        context: "GuardrailContext",
        detector: str,
        category: str,
        action: str,
        metadata: dict | None = None,
    ) -> SecurityEvent:
        """给 Tool 等不走 Detector 的决策写审计事件。"""
        return self.append(
            SecurityEvent(
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                agent=context.agent,
                stage=context.stage.name,
                detector=detector,
                category=category,
                severity="",
                action=action,
                metadata=metadata or {},
            )
        )

    # ---- 读 ---------------------------------------------------------------
    def list(self, limit: int = 50, **filters: str | None) -> list[SecurityEvent]:
        with self._lock:
            items = list(self._events)
        result = items
        for key, value in filters.items():
            if value:
                result = [e for e in result if str(getattr(e, key, "")).lower() == str(value).lower()]
        return result[:limit]

    def count(self) -> int:
        with self._lock:
            return len(self._events)

    def count_by_action(self) -> dict[str, int]:
        with self._lock:
            items = list(self._events)
        counts: dict[str, int] = {}
        for e in items:
            counts[e.action] = counts.get(e.action, 0) + 1
        return counts


__all__ = ["AuditRepository"]
