"""EventService —— 事件入口（设计说明书 §28, §32）。

> Agent -> Event Service -> (Events + Outbox 同事务) -> Outbox Worker -> Queue

关键点：事件写入（§26）不阻塞、不等待任何 Webhook —— ``POST /v1/events``
立即返回 ``{event_id, status: accepted}``（§32）。
"""
from __future__ import annotations

import logging
from typing import Any

from ..domain.event import Event, validate_event_type
from ..domain.exceptions import NotFoundError
from ..domain.outbox import OutboxEntry
from ..metrics import Metrics
from ..storage.repository import Repository

logger = logging.getLogger(__name__)


class EventService:
    def __init__(
        self,
        repo: Repository,
        *,
        tenant_id: str = "tenant_001",
        metrics: Metrics | None = None,
    ) -> None:
        self.repo = repo
        self.tenant_id = tenant_id
        self.metrics = metrics

    async def create_event(
        self,
        type_: str,
        data: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> Event:
        """创建事件 + Outbox 条目，同一数据库事务提交（§26）。

        原子性保证：DB 写入成功 ⇔ Outbox 一定存在 → Queue 一定最终 publish。
        「DB 成功但 Queue 没发」的窗口被消除（实验 7 验证）。
        """
        validate_event_type(type_)
        event = Event.create(
            type_,
            data,
            tenant_id=tenant_id or self.tenant_id,
            metadata=metadata,
        )
        entry = OutboxEntry.create(event_id=event.id)
        await self.repo.create_event_with_outbox(event, entry)
        if self.metrics is not None:
            self.metrics.on_event_created()
        logger.info("event created: %s (%s)", event.id, event.type)
        return event

    async def get_event(self, event_id: str) -> Event:
        event = await self.repo.get_event(event_id)
        if event is None:
            raise NotFoundError(f"Event {event_id} 不存在")
        return event

    async def list_events(self, *, limit: int = 50) -> list[Event]:
        return await self.repo.list_events(limit=limit)
