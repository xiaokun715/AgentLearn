"""Subscriber 领域模型（设计说明书 §06~§07）。

> Subscriber = 谁订阅 + 订阅什么 + 发到哪里 + 怎么验证。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .event import new_id, utcnow


@dataclass(slots=True)
class Subscriber:
    id: str
    tenant_id: str
    url: str
    secret: str
    events: list[str] = field(default_factory=list)
    # active | disabled
    status: str = "active"
    created_at: datetime = field(default_factory=utcnow)

    @classmethod
    def create(
        cls,
        *,
        url: str,
        events: list[str],
        tenant_id: str,
        secret: str,
    ) -> "Subscriber":
        return cls(
            id=new_id("sub"),
            tenant_id=tenant_id,
            url=url,
            secret=secret,
            events=list(events),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "url": self.url,
            "events": self.events,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }
