"""WorkerPool —— 一组 Worker + 一个 Reaper（设计说明书 §14 / §30）。"""
from __future__ import annotations

import asyncio
import logging

from ..config import QueueConfig
from ..executor.job_executor import JobExecutor
from ..observability.metrics import Metrics
from ..queue.base import JobQueue
from ..storage.event_store import EventStore
from ..storage.job_store import JobStore
from .reaper import Reaper
from .worker import ActiveCounter, Worker

logger = logging.getLogger(__name__)


class WorkerPool:
    def __init__(
        self,
        *,
        config: QueueConfig,
        queue: JobQueue,
        executor: JobExecutor,
        job_store: JobStore,
        event_store: EventStore,
        metrics: Metrics,
        start_reaper: bool = True,
    ) -> None:
        self.config = config
        self.queue = queue
        self.executor = executor
        self.job_store = job_store
        self.event_store = event_store
        self.metrics = metrics
        self.start_reaper = start_reaper
        self.active = ActiveCounter()
        self._tasks: list[asyncio.Task] = []
        self._workers: list[Worker] = []
        # 总是创建 Reaper 实例（便于测试手动触发 reap_once），
        # 仅在 start_reaper=True 时把它的循环作为后台任务启动。
        self._reaper = Reaper(
            self.job_store, self.event_store, self.queue, self.metrics,
            interval=self.config.reaper_interval, grace=self.config.reaper_grace,
        )

    async def start(self) -> None:
        for i in range(self.config.worker_count):
            worker = Worker(
                worker_id=f"worker-{i + 1}",
                queue=self.queue,
                executor=self.executor,
                metrics=self.metrics,
                active=self.active,
            )
            self._workers.append(worker)
            self._tasks.append(asyncio.create_task(worker.run()))
        if self.start_reaper:
            self._tasks.append(asyncio.create_task(self._reaper.run()))
        self.metrics.set_gauge("agent_queue_depth", self.queue.depth())
        logger.info("worker pool started with %d workers", self.config.worker_count)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("worker pool stopped")

    @property
    def active_count(self) -> int:
        return self.active.value

    @property
    def reaper(self) -> Reaper | None:
        """故障恢复扫描器（测试可直接触发 reap_once）。"""
        return getattr(self, "_reaper", None)

    @property
    def worker_ids(self) -> list[str]:
        return [w.worker_id for w in self._workers]
