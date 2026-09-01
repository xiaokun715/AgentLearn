"""Dead Letter Queue（设计说明书 §27-28）。

DLQ 用于存放「无法成功执行 + 已超过 Retry 次数」的 Job，避免无限重试把系统拖垮。
管理员可以查看、人工修复、重新入队（POST /v1/dlq/{job_id}/retry）。

DLQ 记录（§28 的 job_id/reason/retry_count/last_error/failed_at）
落在 Job 行（status=DEAD, error, retry_count, finished_at）+ 事件日志上。
"""
from __future__ import annotations

import logging
import time

from ..domain.events import JobEventType
from ..domain.status import JobStatus
from ..observability.metrics import Metrics
from ..queue.base import JobQueue
from ..storage.event_store import EventStore
from ..storage.job_store import JobStore

logger = logging.getLogger(__name__)


class DlqManager:
    def __init__(
        self,
        job_store: JobStore,
        event_store: EventStore,
        queue: JobQueue,
        metrics: Metrics,
    ) -> None:
        self._job_store = job_store
        self._event_store = event_store
        self._queue = queue
        self._metrics = metrics

    async def send(self, job_id: str, *, reason: str, last_error: str) -> None:
        """把 Job 移入 DLQ（status -> DEAD）。"""
        # 从 RUNNING/RETRYING/FAILED 迁移到 DEAD
        from_status = None
        job = await self._job_store.get(job_id)
        if job is None:
            return
        for s in (JobStatus.RETRYING, JobStatus.FAILED, JobStatus.RUNNING):
            ok = await self._job_store.transition(
                job_id, s, JobStatus.DEAD,
                error=f"{reason}: {last_error}" if last_error else reason,
                finished_at=time.time(),
                worker_id=None,
                lease_expire_at=None,
            )
            if ok:
                from_status = s
                break
        if from_status is None:
            return
        await self._event_store.append(
            job_id, JobEventType.JOB_DEAD.value,
            {"reason": reason, "retry_count": job.retry_count, "last_error": last_error},
        )
        self._metrics.inc("agent_jobs_dead_total")
        logger.warning("job %s moved to DLQ (reason=%s)", job_id, reason)

    async def list(self) -> list[dict]:
        jobs = await self._job_store.list_dead()
        return [
            {
                "job_id": j.id,
                "reason": (j.error or "unknown").split(":", 1)[0],
                "retry_count": j.retry_count,
                "max_retries": j.max_retries,
                "last_error": j.error,
                "failed_at": j.finished_at,
                "agent": j.agent_name,
            }
            for j in jobs
        ]

    async def requeue(self, job_id: str) -> bool:
        """人工把 DLQ 中的 Job 重新入队（§28）。

        DEAD -> QUEUED，重置 retry_count / error / worker / lease，然后入队。
        """
        job = await self._job_store.get(job_id)
        if job is None or job.status != JobStatus.DEAD:
            return False
        ok = await self._job_store.transition(
            job_id, JobStatus.DEAD, JobStatus.QUEUED,
            retry_count=0,
            error=None,
            worker_id=None,
            lease_expire_at=None,
            cancel_requested=False,
            current_step=None,
            progress=0,
            queued_at=time.time(),
        )
        if not ok:
            return False
        await self._event_store.append(
            job_id, JobEventType.JOB_REQUEUED.value,
            {"from": "dlq"},
        )
        await self._queue.publish(job_id, priority=job.priority, tenant=job.tenant_id)
        logger.info("job %s requeued from DLQ", job_id)
        return True
