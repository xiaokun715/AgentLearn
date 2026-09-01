"""审计日志（设计说明书 §33）。

所有执行都记录：tenant / user / agent / execution_id / tool / policy / image / command /
start / end / exit_code / resource_usage / network_policy / result。

对应 §34 audit_events 表。生产环境把 ``emit`` 接到数据库即可；
这里先落内存 + 结构化日志，保证 demo 可审计。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AuditEvent:
    execution_id: str
    event_type: str          # execution.created / rejected / finished / killed ...
    tenant_id: str = ""
    user_id: str = ""
    agent_id: str = ""
    payload: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "event_type": self.event_type,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


class AuditLogger:
    """内存版审计记录器：快照可查 + 结构化日志落盘。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._events: list[AuditEvent] = []

    def emit(
        self,
        *,
        execution_id: str,
        event_type: str,
        identity: "Identity | None" = None,
        payload: dict | None = None,
    ) -> None:
        if not self.enabled:
            return
        event = AuditEvent(
            execution_id=execution_id,
            event_type=event_type,
            tenant_id=identity.tenant_id if identity else "",
            user_id=identity.user_id if identity else "",
            agent_id=identity.agent_id if identity else "",
            payload=payload or {},
        )
        self._events.append(event)
        logger.info(
            "audit %s execution=%s tenant=%s tool=%s payload=%s",
            event.event_type, event.execution_id, event.tenant_id,
            event.payload.get("tool", "?"), event.payload,
        )

    def snapshot(self) -> list[dict]:
        return [e.to_dict() for e in self._events]
