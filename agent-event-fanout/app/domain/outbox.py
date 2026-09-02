"""Outbox 领域模型（设计说明书 §25~§27）。

> Outbox Pattern：Event 与 Outbox 在同一数据库事务中提交，保证
> 「DB 成功 ⇔ Queue 一定会 publish」，绝不让 Event 丢失（§26）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .event import new_id, utcnow

OUTBOX_PENDING = "PENDING"
OUTBOX_PROCESSING = "PROCESSING"
OUTBOX_PUBLISHED = "PUBLISHED"


@dataclass(slots=True)
class OutboxEntry:
    id: str
    event_id: str
    status: str = OUTBOX_PENDING
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    published_at: datetime | None = None

    @classmethod
    def create(cls, *, event_id: str) -> "OutboxEntry":
        return cls(id=new_id("obx"), event_id=event_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }
