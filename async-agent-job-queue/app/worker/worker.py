"""Worker —— 只负责「消费任务」（设计说明书 §14-15）。"""
from __future__ import annotations

import logging

from ..executor.job_executor import JobExecutor
from ..observability.metrics import Metrics
from ..queue.base import JobQueue

logger = logging.getLogger(__name__)


class ActiveCounter:
    """共享的「忙碌 Worker 数」计数器（agent_worker_active 指标用）。"""

    def __init__(self) -> None:
        self.value = 0

    def inc(self) -> None:
        self.value += 1

    def dec(self) -> None:
        self.value = max(0, self.value - 1)


class Worker:
    def __init__(
        self,
        worker_id: str,
        queue: JobQueue,
        executor: JobExecutor,
        metrics: Metrics,
        active: ActiveCounter,
    ) -> None:
        self.worker_id = worker_id
        self.queue = queue
        self.executor = executor
        self.metrics = metrics
        self.active = active

    async def run(self) -> None:
        """消费循环：get -> execute -> ack。"""
        logger.info("worker %s started", self.worker_id)
        while True:
            job_id = await self.queue.get()  # 阻塞；被取消时抛 CancelledError
            self.active.inc()
            self.metrics.set_gauge("agent_worker_active", self.active.value)
            try:
                await self.executor.execute(self.worker_id, job_id)
            except Exception:  # noqa: BLE001  JobExecutor 已兜底，这里防漏网
                logger.exception("worker %s unexpected error on job %s",
                                 self.worker_id, job_id)
            finally:
                await self.queue.ack(job_id)
                self.active.dec()
                self.metrics.set_gauge("agent_worker_active", self.active.value)
