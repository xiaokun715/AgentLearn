"""内存版 JobStore / EventStore —— 单进程 Demo 用（设计说明书 §11-12）。

注意：内存版只用于「理解机制」。进程崩溃后 Job / 事件全部丢失，
这正是 §12 强调的 asyncio.Queue 致命问题 —— 生产要用 SQLite/PostgreSQL/Redis。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..domain.events import JobEvent
from ..domain.job import Job
from ..domain.status import JobStatus
from .event_store import EventStore
from .job_store import JobStore


class MemoryJobStore(JobStore):
    """以 ``dict[job_id, Job]`` + asyncio.Lock 实现。

    所有读-改-写都在锁内完成，并用 ``JobStateMachine.assert_can_transition``
    校验迁移合法性，模拟数据库条件更新的语义。
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    async def create(self, job: Job) -> Job:
        async with self._lock:
            self._jobs[job.id] = job
            return job

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job: Job) -> Job:
        async with self._lock:
            self._jobs[job.id] = job
            return job

    async def transition(
        self, job_id: str, from_status: JobStatus, to_status: JobStatus, **fields: Any
    ) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != from_status:
                return False
            # domain 层状态机校验（存储层双保险）
            from ..domain.state_machine import JobStateMachine

            JobStateMachine.assert_can_transition(from_status, to_status)
            for k, v in fields.items():
                setattr(job, k, v)
            job.status = to_status
            job.updated_at = time.time()
            return True

    async def acquire_lease(self, job_id: str, worker_id: str, duration: float) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            now = time.time()
            leased = job.lease_expire_at is not None and job.lease_expire_at > now
            if leased and job.worker_id != worker_id:
                return False  # 被别人持有且未过期
            job.worker_id = worker_id
            job.lease_expire_at = now + duration
            job.updated_at = now
            return True

    async def renew_lease(self, job_id: str, worker_id: str, duration: float) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.worker_id != worker_id:
                return False
            job.lease_expire_at = time.time() + duration
            job.updated_at = time.time()
            return True

    async def release_lease(self, job_id: str, worker_id: str) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.worker_id != worker_id:
                return False
            job.worker_id = None
            job.lease_expire_at = None
            job.updated_at = time.time()
            return True

    async def expire_lease(self, job_id: str, worker_id: str) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.worker_id != worker_id:
                return False
            job.lease_expire_at = time.time() - 1.0  # 立即过期
            job.updated_at = time.time()
            return True

    async def update_progress(
        self, job_id: str, worker_id: str, *, step: str | None, progress: int
    ) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.worker_id != worker_id:
                return False
            if step is not None:
                job.current_step = step
            job.progress = progress
            job.updated_at = time.time()
            return True

    async def find_recoverable(self, now: float, grace: float) -> list[Job]:
        async with self._lock:
            out = []
            for job in self._jobs.values():
                if job.status in (JobStatus.RUNNING, JobStatus.RETRYING):
                    if job.lease_expire_at is None:
                        continue
                    if job.lease_expire_at + grace < now:
                        out.append(job)
            return list(out)

    async def set_cancel_requested(self, job_id: str, value: bool = True) -> bool:
        async with self._lock:
            if job_id not in self._jobs:
                return False
            if value:
                self._cancelled.add(job_id)
            else:
                self._cancelled.discard(job_id)
            return True

    async def is_cancel_requested(self, job_id: str) -> bool:
        return job_id in self._cancelled

    async def list_dead(self) -> list[Job]:
        async with self._lock:
            return [j for j in self._jobs.values() if j.status == JobStatus.DEAD]

    async def count_by_status(self) -> dict[str, int]:
        async with self._lock:
            counts: dict[str, int] = {}
            for j in self._jobs.values():
                counts[j.status.value] = counts.get(j.status.value, 0) + 1
            return counts

    async def close(self) -> None:
        return None


class MemoryEventStore(EventStore):
    """内存事件日志：``list[JobEvent]``。"""

    def __init__(self) -> None:
        self._events: dict[str, list[JobEvent]] = {}

    async def append(self, job_id: str, event_type: str, payload: dict | None = None) -> JobEvent:
        ev = JobEvent(job_id=job_id, event_type=event_type, payload=payload)
        ev.seq = len(self._events.get(job_id, [])) + 1
        self._events.setdefault(job_id, []).append(ev)
        return ev

    async def list(self, job_id: str) -> list[JobEvent]:
        return list(self._events.get(job_id, []))

    async def close(self) -> None:
        return None
