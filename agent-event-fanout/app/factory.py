"""依赖装配（工厂模式）—— 按配置组装所有组件，供 main.py / 测试 / 脚本复用。

默认零外部依赖：SQLite + 内存队列即可跑通全部 8 个实验（设计说明书 §46）。
PostgreSQL / Redis 通过环境变量切换（见 docker-compose.yml）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .config import EventFanoutConfig
from .dlq.dlq_service import DLQService
from .event.event_service import EventService
from .event.fanout_service import FanoutConsumer, FanoutService
from .metrics import Metrics
from .queue.base import EventQueue
from .storage.repository import Repository
from .webhook.retry import RetryPolicy
from .webhook.sender import WebhookSender
from .webhook.signer import Signer
from .workers.outbox_worker import OutboxWorker
from .workers.webhook_worker import WebhookWorker

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    """一次性持有全部组件，方便 API / 测试 / 脚本共用。"""

    config: EventFanoutConfig
    repo: Repository
    event_queue: EventQueue
    delivery_queue: EventQueue
    event_service: EventService
    fanout_service: FanoutService
    fanout_consumer: FanoutConsumer
    outbox_worker: OutboxWorker
    webhook_worker: WebhookWorker
    dlq_service: DLQService
    signer: Signer
    sender: WebhookSender
    retry_policy: RetryPolicy
    metrics: Metrics = field(default_factory=Metrics)
    _closers: list = field(default_factory=list)
    _tasks: list[asyncio.Task] = field(default_factory=list)

    async def start(self) -> None:
        """启动后台 Worker（outbox -> fanout -> webhook）。测试可跳过。"""
        self._tasks = [
            asyncio.create_task(self.outbox_worker.run(), name="outbox"),
            asyncio.create_task(self.fanout_consumer.run(), name="fanout"),
            asyncio.create_task(self.webhook_worker.run(), name="webhook"),
        ]
        logger.info("workers started: outbox / fanout / webhook")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
        for closer in reversed(self._closers):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                logger.exception("close failed")


async def build_runtime(config: EventFanoutConfig | None = None) -> Runtime:
    """构造完整运行环境。PostgreSQL / Redis 后端未装依赖时抛清晰的错误。"""
    config = config or EventFanoutConfig.from_env()
    closers: list = []

    # ---- 存储层 -------------------------------------------------------------
    if config.storage_backend == "postgres":
        try:
            from .storage.postgres import PostgresDatabase, PostgresRepository
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "STORAGE_BACKEND=postgres 需要 pip install agent-event-fanout[postgres]"
            ) from e
        db = PostgresDatabase(config.database_url)
        await db.connect()
        repo: Repository = PostgresRepository(db)
        closers.append(db.close)
    else:  # sqlite（默认，零外部依赖）
        try:
            from .storage.repository import SqliteDatabase, SqliteRepository
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "默认 sqlite 后端需要 pip install agent-event-fanout[sqlite]"
            ) from e
        db = SqliteDatabase(config.sqlite_path)
        await db.connect()
        repo = SqliteRepository(db)
        closers.append(db.close)

    # ---- 队列层 -------------------------------------------------------------
    if config.queue_backend == "redis":
        try:
            from .queue.redis_queue import RedisQueue
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "QUEUE_BACKEND=redis 需要 pip install agent-event-fanout[redis]"
            ) from e
        event_queue: EventQueue = RedisQueue("events", config.redis_url)
        delivery_queue: EventQueue = RedisQueue("deliveries", config.redis_url)
        closers.extend([event_queue.close, delivery_queue.close])
    else:  # memory（默认）
        from .queue.memory import MemoryQueue

        event_queue = MemoryQueue("events")
        delivery_queue = MemoryQueue("deliveries")

    # ---- 业务层 -------------------------------------------------------------
    metrics = Metrics()
    retry_policy: RetryPolicy = config.retry
    signer = Signer(tolerance=config.signature_tolerance)
    sender = WebhookSender(timeout=config.request_timeout)
    closers.append(sender.close)

    event_service = EventService(repo, tenant_id=config.tenant_id, metrics=metrics)
    fanout_service = FanoutService(
        repo, delivery_queue, tenant_id=config.tenant_id, metrics=metrics
    )
    fanout_consumer = FanoutConsumer(
        fanout_service, event_queue,
        poll_interval=config.outbox_poll_interval,
    )
    outbox_worker = OutboxWorker(
        repo, event_queue,
        poll_interval=config.outbox_poll_interval,
        batch_size=config.outbox_batch_size,
    )
    dlq_service = DLQService(repo, delivery_queue)
    webhook_worker = WebhookWorker(
        repo, delivery_queue,
        signer=signer,
        sender=sender,
        retry_policy=retry_policy,
        dlq=dlq_service,
        metrics=metrics,
        poll_interval=config.webhook_poll_interval,
        batch_size=config.webhook_batch_size,
    )

    return Runtime(
        config=config,
        repo=repo,
        event_queue=event_queue,
        delivery_queue=delivery_queue,
        event_service=event_service,
        fanout_service=fanout_service,
        fanout_consumer=fanout_consumer,
        outbox_worker=outbox_worker,
        webhook_worker=webhook_worker,
        dlq_service=dlq_service,
        signer=signer,
        sender=sender,
        retry_policy=retry_policy,
        metrics=metrics,
        _closers=closers,
    )
