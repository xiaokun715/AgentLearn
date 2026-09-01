"""WebhookWorker —— 投递执行器（设计说明书 §34, §10, §16~§20）。

职责（§34）：:

    Event
     ↓ 查 Subscriber
     ↓ 生成签名
     ↓ HTTP POST
     ↓ 判断成功/失败
     ↓ Retry / Success / DLQ

两条投递路径共享 ``deliver_claimed``：
- **队列路径**：消费「deliveries」队列中的 delivery_id（首次投递，§33）。
- **DB 扫描路径**：``process_due`` 领取到期（RETRYING 且 next_retry_at <= now）
  的 Delivery —— 这是重试的持久化调度器（§17 指数退避由 next_retry_at 体现）。

原子领取（claim）保证实验 5：同一 Delivery 被重复消费时，只有一次真正执行。
"""
from __future__ import annotations

import asyncio
import logging

import time

from ..domain.delivery import DLQ, Delivery
from ..domain.event import utcnow
from ..dlq.dlq_service import DLQService
from ..metrics import Metrics
from ..queue.base import EventQueue
from ..storage.repository import Repository
from ..webhook.retry import RetryPolicy
from ..webhook.sender import WebhookSender
from ..webhook.serializer import serialize_event
from ..webhook.signer import Signer

logger = logging.getLogger(__name__)


class WebhookWorker:
    def __init__(
        self,
        repo: Repository,
        delivery_queue: EventQueue,
        *,
        signer: Signer,
        sender: WebhookSender,
        retry_policy: RetryPolicy,
        dlq: DLQService,
        metrics: Metrics | None = None,
        poll_interval: float = 0.5,
        batch_size: int = 10,
    ) -> None:
        self.repo = repo
        self.delivery_queue = delivery_queue
        self.signer = signer
        self.sender = sender
        self.retry_policy = retry_policy
        self.dlq = dlq
        self.metrics = metrics
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    # ---- 入口 1：队列触发（首次投递） ---------------------------------------
    async def deliver_delivery(self, delivery_id: str) -> Delivery | None:
        """从「deliveries」队列取出 delivery_id 后调用。

        原子领取失败（已被其他 Worker 领取 / 已是终态）返回 None。
        """
        delivery = await self.repo.claim_delivery(delivery_id, utcnow())
        if delivery is None:
            return None
        return await self.deliver_claimed(delivery)

    # ---- 入口 2：DB 重试扫描 -------------------------------------------------
    async def process_due(self) -> int:
        """领取并投递一批到期（PENDING/RETRYING 且到期）的 Delivery。"""
        due = await self.repo.claim_due_deliveries(self.batch_size, utcnow())
        for delivery in due:
            try:
                await self.deliver_claimed(delivery)
            except Exception:  # noqa: BLE001
                logger.exception("deliver due %s failed", delivery.id)
        return len(due)

    # ---- 核心：投递一个已领取（DELIVERING）的 Delivery -----------------------
    async def deliver_claimed(self, delivery: Delivery) -> Delivery:
        event = await self.repo.get_event(delivery.event_id)
        subscriber = await self.repo.get_subscriber(delivery.subscriber_id)
        if event is None or subscriber is None:
            # 数据不一致：进入 DLQ，避免无限重试
            delivery.mark_failed("event 或 subscriber 不存在", None)
            delivery.dead_letter("event 或 subscriber 已被删除")
            await self.repo.update_delivery(delivery)
            await self.dlq.on_dead_letter(delivery, "数据不一致")
            return delivery

        body = serialize_event(event)
        headers = self.signer.build_headers(
            secret=subscriber.secret,
            event_id=event.id,
            event_type=event.type,
            delivery_id=delivery.id,
            body=body,
        )

        started = time.perf_counter()
        outcome = await self.sender.post(
            url=subscriber.url, headers=headers, body=body
        )
        if self.metrics is not None:
            self.metrics.on_attempt(started)
        logger.debug(
            "deliver %s -> %s: %s", delivery.id, subscriber.url, outcome.summary
        )

        if outcome.success:
            delivery.mark_success(outcome.status_code)
            await self.repo.update_delivery(delivery)
            if self.metrics is not None:
                self.metrics.on_result(success=True, retried=False, dlq=False)
            return delivery

        # 失败：判断是否重试（§18）
        delivery.mark_failed(outcome.error or "unknown", outcome.status_code)

        exhausted = self.retry_policy.is_exhausted(delivery.attempt_count)
        if outcome.retryable and not exhausted:
            # §17 指数退避 + 抖动 / §38 Retry-After
            next_at = self.retry_policy.compute_next_retry_at(
                attempt=delivery.attempt_count, retry_after=outcome.retry_after
            )
            delivery.schedule_retry(next_at)
            await self.repo.update_delivery(delivery)
            if self.metrics is not None:
                self.metrics.on_result(success=False, retried=True, dlq=False)
            return delivery

        # §20：不可重试或超过 max_attempts -> DLQ
        reason = (
            f"超过 max_attempts={self.retry_policy.max_attempts}"
            if exhausted
            else f"不可重试: {outcome.summary}"
        )
        delivery.dead_letter(reason)
        await self.repo.update_delivery(delivery)
        await self.dlq.on_dead_letter(delivery, reason)
        if self.metrics is not None:
            self.metrics.on_result(success=False, retried=False, dlq=True)
        return delivery

    # ---- 常驻循环 ------------------------------------------------------------
    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                # 1) 消费队列（首次投递）
                for _ in range(self.batch_size):
                    delivery_id = await self.delivery_queue.pop(timeout=0.05)
                    if delivery_id is None:
                        break
                    await self.deliver_delivery(delivery_id)
                # 2) DB 重试扫描（退避到期的 Delivery）
                await self.process_due()
            except Exception:  # noqa: BLE001
                logger.exception("webhook worker loop failed")
            await asyncio.sleep(self.poll_interval)
