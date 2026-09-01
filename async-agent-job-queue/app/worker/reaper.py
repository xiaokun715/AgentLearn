"""Reaper —— 故障恢复的关键角色（设计说明书 §30-31 / §55 问题3）。

问题：Worker Crash 后，谁负责发现任务需要恢复？
答案：Reaper。它定期扫描 RUNNING/RETRYING 且租约已过期的 Job，
把状态重置回 QUEUED 并重新入队；其他 Worker 接管后从 Checkpoint 断点续跑。
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..domain.events import JobEventType
from ..domain.status import JobStatus
from ..observability.metrics import Metrics
from ..queue.base import JobQueue
from ..storage.event_store import EventStore
from ..storage.job_store import JobStore

logger = logging.getLogger(__name__)


class Reaper:
    def __init__(
        self,
        job_store: JobStore,
        event_store: EventStore,
        queue: JobQueue,
        metrics: Metrics,
        *,
        interval: float,
        grace: float,
    ) -> None:
        self.job_store = job_store
        self.event_store = event_store
        self.queue = queue
        self.metrics = metrics
        self.interval = interval
        self.grace = grace

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.reap_once()
            except Exception:  # noqa: BLE001
                logger.exception("reaper sweep failed")

    async def reap_once(self) -> int:
        """执行一轮恢复扫描，返回被重新入队的 Job 数量。"""
        now = time.time()
        recoverable = await self.job_store.find_recoverable(now, self.grace)
        recovered = 0
        for job in recoverable:
            if job.status not in (JobStatus.RUNNING, JobStatus.RETRYING):
                continue
            # 若恢复时已请求取消，则直接终止而不是重新入队（§10）
            if await self.job_store.is_cancel_requested(job.id):
                await self.job_store.transition(
                    job.id, job.status, JobStatus.CANCELLED,
                    worker_id=None, lease_expire_at=None, cancel_requested=False,
                    finished_at=now, error="cancelled",
                )
                await self.event_store.append(
                    job.id, JobEventType.JOB_CANCELLED.value,
                    {"reason": "cancelled_during_recovery"},
                )
                self.metrics.inc("agent_jobs_cancelled_total")
                continue
            ok = await self.job_store.transition(
                job.id, job.status, JobStatus.QUEUED,
                worker_id=None, lease_expire_at=None,
                queued_at=now, cancel_requested=False,
                error=(job.error or "recovered: lease expired"),
            )
            if not ok:
                continue
            await self.event_store.append(
                job.id, JobEventType.JOB_RECOVERED.value,
                {"from": job.status.value, "previous_worker": job.worker_id,
                 "stalled_seconds": round(now - (job.updated_at or now), 2)},
            )
            self.metrics.inc("agent_jobs_recovered_total")
            self.metrics.inc("agent_jobs_requeued_total")
            await self.queue.publish(job.id, priority=job.priority, tenant=job.tenant_id)
            recovered += 1
            logger.warning("job %s recovered (lease expired), requeued", job.id)
        return recovered
