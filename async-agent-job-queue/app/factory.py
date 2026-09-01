"""应用装配（工厂模式）—— 按配置组装所有组件。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .agent.llm import MockLLM
from .agent.registry import AgentRegistry
from .checkpoint.base import CheckpointStore
from .config import QueueConfig
from .dlq.manager import DlqManager
from .executor.job_executor import JobExecutor
from .observability.metrics import Metrics
from .observability.tracing import Tracer
from .queue.base import JobQueue
from .retry.policy import RetryPolicy
from .service import DlqService, JobService
from .storage.event_store import EventStore
from .storage.job_store import JobStore
from .worker.pool import WorkerPool


@dataclass
class Runtime:
    """一次性持有全部组件，方便 API / 测试 / 脚本共用。"""

    config: QueueConfig
    job_store: JobStore
    event_store: EventStore
    checkpoint_store: CheckpointStore
    queue: JobQueue
    agent_registry: AgentRegistry
    dlq_manager: DlqManager
    executor: JobExecutor
    job_service: JobService
    dlq_service: DlqService
    metrics: Metrics
    tracer: Tracer
    pool: WorkerPool | None = None
    _closers: list = field(default_factory=list)

    async def start(self) -> None:
        if self.pool is not None:
            await self.pool.start()

    async def stop(self) -> None:
        if self.pool is not None:
            await self.pool.stop()
        await self.executor.close()  # 取消未触发的延迟重投任务
        for closer in reversed(self._closers):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception("close failed")


async def build_runtime(
    config: QueueConfig | None = None, *, start_reaper: bool = True
) -> Runtime:
    """构造完整运行环境。PostgreSQL/Redis 后端未装依赖时抛清晰的错误。

    ``start_reaper`` 为 False 时只建 Reaper 实例但不自动后台扫描，
    便于测试手动调用 ``runtime.pool.reaper.reap_once()`` 做确定性恢复。
    """
    config = config or QueueConfig.from_env()
    metrics = Metrics()
    tracer = Tracer()

    # ---- 存储层 -------------------------------------------------------------
    if config.storage_backend == "memory":
        from .checkpoint.memory import MemoryCheckpointStore
        from .storage.memory import MemoryEventStore, MemoryJobStore

        job_store: JobStore = MemoryJobStore()
        event_store: EventStore = MemoryEventStore()
        checkpoint: CheckpointStore = MemoryCheckpointStore()
        closers: list = []

    elif config.storage_backend == "postgres":
        try:
            from .checkpoint.postgres import PostgresCheckpointStore
            from .storage.postgres import (
                PostgresDatabase,
                PostgresEventStore,
                PostgresJobStore,
            )
        except ImportError as e:
            raise RuntimeError(
                "STORAGE_BACKEND=postgres 需要 pip install async-agent-job-queue[postgres]"
            ) from e
        db = PostgresDatabase(config.database_url)
        await db.connect()
        job_store = PostgresJobStore(db)
        event_store = PostgresEventStore(db)
        checkpoint = PostgresCheckpointStore(db)
        closers = [db.close]

    else:  # sqlite（默认）
        from .checkpoint.sqlite import SqliteCheckpointStore
        from .storage.sqlite import SqliteDatabase, SqliteEventStore, SqliteJobStore

        db = SqliteDatabase(config.sqlite_path)
        await db.connect()
        job_store = SqliteJobStore(db)
        event_store = SqliteEventStore(db)
        checkpoint = SqliteCheckpointStore(db)
        closers = [db.close]

    # ---- 队列层 -------------------------------------------------------------
    if config.queue_backend == "redis":
        try:
            from .queue.redis import RedisStreamsQueue

            queue: JobQueue = RedisStreamsQueue(config.redis_url)
        except ImportError as e:
            raise RuntimeError(
                "QUEUE_BACKEND=redis 需要 pip install async-agent-job-queue[redis]"
            ) from e
    else:
        from .queue.memory import MemoryQueue

        queue = MemoryQueue(fair=config.fair_scheduling)

    # ---- 业务层 -------------------------------------------------------------
    agent_registry = AgentRegistry()
    dlq_manager = DlqManager(job_store, event_store, queue, metrics)
    executor = JobExecutor(
        config=config,
        job_store=job_store,
        event_store=event_store,
        checkpoint_store=checkpoint,
        queue=queue,
        agent_registry=agent_registry,
        dlq_manager=dlq_manager,
        retry_policy=RetryPolicy(),
        llm=MockLLM(),
        metrics=metrics,
    )
    job_service = JobService(
        job_store=job_store, event_store=event_store, queue=queue,
        agent_registry=agent_registry, metrics=metrics,
    )
    dlq_service = DlqService(dlq_manager=dlq_manager)

    pool = WorkerPool(
        config=config, queue=queue, executor=executor,
        job_store=job_store, event_store=event_store, metrics=metrics,
        start_reaper=start_reaper,
    )

    return Runtime(
        config=config,
        job_store=job_store,
        event_store=event_store,
        checkpoint_store=checkpoint,
        queue=queue,
        agent_registry=agent_registry,
        dlq_manager=dlq_manager,
        executor=executor,
        job_service=job_service,
        dlq_service=dlq_service,
        metrics=metrics,
        tracer=tracer,
        pool=pool,
        _closers=closers,
    )
