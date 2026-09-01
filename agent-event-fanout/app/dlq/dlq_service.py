"""DLQService —— 死信队列管理（设计说明书 §20~§21, §23~§24）。

> FAILED -> max_attempts -> DLQ（§20）。管理员可以查看 / Replay / 取消（§21）。

DLQ 的实现即 ``webhook_deliveries.status = 'DLQ'``，保存的信息与 §21 一致：
delivery_id / event_id / subscriber_id / attempt_count / last_error / failed_at。
"""
from __future__ import annotations

import logging

from ..domain.delivery import CANCELLED, DLQ, PENDING, Delivery
from ..domain.exceptions import NotFoundError
from ..queue.base import EventQueue
from ..storage.repository import Repository

logger = logging.getLogger(__name__)


class DLQService:
    def __init__(self, repo: Repository, delivery_queue: EventQueue) -> None:
        self.repo = repo
        self.delivery_queue = delivery_queue

    async def on_dead_letter(self, delivery: Delivery, reason: str) -> None:
        """Delivery 进入 DLQ 时的钩子（当前记录日志，可扩展为告警/审计）。"""
        logger.warning(
            "DLQ: delivery=%s event=%s subscriber=%s attempts=%d last_error=%s",
            delivery.id, delivery.event_id, delivery.subscriber_id,
            delivery.attempt_count, delivery.last_error,
        )

    async def list_dlq(self, *, limit: int = 100) -> list[Delivery]:
        """查看 DLQ（§21：管理员可以查看）。"""
        return await self.repo.list_deliveries(status=DLQ, limit=limit)

    async def replay(self, delivery_id: str) -> Delivery:
        """DLQ -> PENDING 并重新入队（§21：Replay）。"""
        delivery = await self._require_dlq(delivery_id)
        delivery.replay()
        await self.repo.update_delivery(delivery)
        await self.delivery_queue.publish(delivery.id)
        logger.info("DLQ replay: %s -> PENDING", delivery_id)
        return delivery

    async def cancel(self, delivery_id: str) -> Delivery:
        """DLQ -> CANCELLED（§21：取消）。"""
        delivery = await self._require_dlq(delivery_id)
        delivery.cancel()
        await self.repo.update_delivery(delivery)
        return delivery

    async def _require_dlq(self, delivery_id: str) -> Delivery:
        delivery = await self.repo.get_delivery(delivery_id)
        if delivery is None:
            raise NotFoundError(f"Delivery {delivery_id} 不存在")
        if delivery.status != DLQ:
            raise NotFoundError(
                f"Delivery {delivery_id} 状态为 {delivery.status}，不是 DLQ"
            )
        return delivery


__all__ = ["DLQService", "PENDING", "DLQ", "CANCELLED"]
