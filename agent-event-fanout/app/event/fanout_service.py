"""FanoutService —— 事件扇出（设计说明书 §08, §33）。

> 一个 Event (1) -> N 个 Delivery（每个匹配的 Subscriber 一个）。

依赖 Fan-out 队列：Fanout 消费「events」队列中的 event_id，
为每个匹配 Subscriber 创建 Delivery（PENDING）并入「deliveries」队列。
"""
from __future__ import annotations

import asyncio
import logging

from ..domain.delivery import Delivery
from ..domain.exceptions import ConflictError, NotFoundError
from ..domain.subscriber import Subscriber
from ..metrics import Metrics
from ..queue.base import EventQueue
from ..storage.repository import Repository

logger = logging.getLogger(__name__)


class FanoutService:
    def __init__(
        self,
        repo: Repository,
        delivery_queue: EventQueue,
        *,
        tenant_id: str = "tenant_001",
        metrics: Metrics | None = None,
    ) -> None:
        self.repo = repo
        self.delivery_queue = delivery_queue
        self.tenant_id = tenant_id
        self.metrics = metrics

    async def fanout_event(self, event_id: str) -> int:
        """为一个 Event 创建所有匹配的 Delivery 并入队（§33）。

        返回新建的 Delivery 数量。已存在的 (event_id, subscriber_id)
        会被 UNIQUE 约束幂等跳过（实验 5）。
        """
        event = await self.repo.get_event(event_id)
        if event is None:
            raise NotFoundError(f"Event {event_id} 不存在")
        subscribers = await self.repo.match_subscribers(
            event.type, tenant_id=event.tenant_id
        )
        created = 0
        for sub in subscribers:
            try:
                delivery = Delivery.create(
                    event_id=event.id, subscriber_id=sub.id
                )
                await self.repo.create_delivery(delivery)
            except ConflictError:
                # 同一 (event_id, subscriber_id) 已存在 → 幂等跳过（§09）
                logger.debug("skip duplicate delivery for %s -> %s", event.id, sub.id)
                continue
            await self.delivery_queue.publish(delivery.id)
            if self.metrics is not None:
                self.metrics.on_delivery_created()
            created += 1
        logger.info("fanout %s -> %d deliveries", event_id, created)
        return created

    async def fanout_event_for(
        self, event_id: str, subscriber: Subscriber
    ) -> Delivery | None:
        """为单个 Subscriber 创建 Delivery（供实验/DLQ 定向重发用）。"""
        try:
            delivery = Delivery.create(event_id=event_id, subscriber_id=subscriber.id)
            await self.repo.create_delivery(delivery)
        except ConflictError:
            return None
        await self.delivery_queue.publish(delivery.id)
        return delivery


class FanoutConsumer:
    """消费「events」队列，把事件扇出为多个 Delivery（§33 的 Worker）。"""

    def __init__(
        self,
        fanout: FanoutService,
        event_queue: EventQueue,
        *,
        poll_interval: float = 0.5,
        batch_size: int = 10,
    ) -> None:
        self.fanout = fanout
        self.event_queue = event_queue
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def drain_once(self) -> int:
        n = 0
        for _ in range(self.batch_size):
            event_id = await self.event_queue.pop(timeout=0.05)
            if event_id is None:
                break
            await self.fanout.fanout_event(event_id)
            n += 1
        return n

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.drain_once()
            except Exception:  # noqa: BLE001
                logger.exception("fanout drain failed")
            await asyncio.sleep(self.poll_interval)
