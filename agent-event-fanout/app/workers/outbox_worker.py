"""OutboxWorker —— Outbox 发布器（设计说明书 §26~§28）。

轮询 ``outbox_events WHERE status=PENDING``，把 event_id publish 到
「events」队列，成功后标记 PUBLISHED；失败回滚为 PENDING 稍后重试。

这保证：Event 一旦写库成功，最终一定进入队列（§26 Outbox Pattern）。
"""
from __future__ import annotations

import asyncio
import logging

from ..domain.outbox import OUTBOX_PENDING, OUTBOX_PUBLISHED
from ..queue.base import EventQueue
from ..storage.repository import Repository

logger = logging.getLogger(__name__)


class OutboxWorker:
    def __init__(
        self,
        repo: Repository,
        event_queue: EventQueue,
        *,
        poll_interval: float = 0.5,
        batch_size: int = 50,
    ) -> None:
        self.repo = repo
        self.event_queue = event_queue
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def drain_once(self) -> int:
        """发布一批待发布 Outbox，返回成功发布数。"""
        entries = await self.repo.claim_outbox_entries(self.batch_size)
        published = 0
        for entry in entries:
            try:
                await self.event_queue.publish(entry.event_id)
                await self.repo.mark_outbox(entry.id, OUTBOX_PUBLISHED)
                published += 1
            except Exception:  # noqa: BLE001
                # Queue publish 失败：回滚为 PENDING，避免事件丢失（§26）
                await self.repo.mark_outbox(entry.id, OUTBOX_PENDING)
                logger.exception("outbox publish failed, rollback %s", entry.id)
        if published:
            logger.info("outbox published %d event(s)", published)
        return published

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.drain_once()
            except Exception:  # noqa: BLE001
                logger.exception("outbox drain failed")
            await asyncio.sleep(self.poll_interval)
