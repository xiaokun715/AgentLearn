"""JobService / DlqService —— API 与底层组件之间的编排层。"""
from __future__ import annotations

import time

from .agent.registry import AgentRegistry
from .dlq.manager import DlqManager
from .domain.events import JobEvent, JobEventType
from .domain.job import Job, new_job_id
from .domain.status import JobStatus
from .observability.metrics import Metrics
from .queue.base import JobQueue
from .storage.event_store import EventStore
from .storage.job_store import JobStore


class JobNotFoundError(Exception):
    pass


class AgentNotFoundError(Exception):
    pass


class JobService:
    def __init__(
        self,
        *,
        job_store: JobStore,
        event_store: EventStore,
        queue: JobQueue,
        agent_registry: AgentRegistry,
        metrics: Metrics,
    ) -> None:
        self._job_store = job_store
        self._event_store = event_store
        self._queue = queue
        self._registry = agent_registry
        self._metrics = metrics

    # ---- 创建（§8：立即返回，绝不等 Agent 执行完） ------------------------------

    async def create(
        self,
        *,
        agent: str,
        input: dict,
        tenant_id: str = "default",
        priority: int = 0,
        max_retries: int = 3,
    ) -> Job:
        if agent not in self._registry.names():
            raise AgentNotFoundError(
                f"unknown agent {agent!r}, available: {self._registry.names()}"
            )
        job = Job.create(
            agent_name=agent,
            input=input,
            tenant_id=tenant_id,
            priority=priority,
            max_retries=max_retries,
            id=new_job_id(),
        )
        await self._job_store.create(job)
        await self._event_store.append(
            job.id, JobEventType.JOB_CREATED.value,
            {"agent": agent, "tenant_id": tenant_id, "priority": priority},
        )
        self._metrics.inc("agent_jobs_created_total")
        await self._queue.publish(job.id, priority=job.priority, tenant=job.tenant_id)
        return job

    async def get(self, job_id: str) -> Job:
        job = await self._job_store.get(job_id)
        if job is None:
            raise JobNotFoundError(f"job not found: {job_id}")
        return job

    # ---- 取消（§10：不是简单改 DB 状态，而是协作式信号） ------------------------

    async def cancel(self, job_id: str) -> Job:
        job = await self.get(job_id)
        if job.status == JobStatus.QUEUED:
            ok = await self._job_store.transition(
                job_id, JobStatus.QUEUED, JobStatus.CANCELLED,
                finished_at=time.time(), error="cancelled",
            )
            if ok:
                await self._event_store.append(
                    job_id, JobEventType.JOB_CANCELLED.value, {"state": "queued"}
                )
                self._metrics.inc("agent_jobs_cancelled_total")
        elif job.status in (JobStatus.RUNNING, JobStatus.RETRYING):
            # 置位取消信号 -> Worker 在下一个执行边界感知并协作停止
            await self._job_store.set_cancel_requested(job_id, True)
        # 其余终态：已经结束，直接返回当前状态
        return await self.get(job_id)

    async def events(self, job_id: str) -> list[JobEvent]:
        await self.get(job_id)
        return await self._event_store.list(job_id)

    async def queue_depth(self) -> int:
        return self._queue.depth()


class DlqService:
    def __init__(self, dlq_manager: DlqManager) -> None:
        self._manager = dlq_manager

    async def list(self) -> list[dict]:
        return await self._manager.list()

    async def retry(self, job_id: str) -> bool:
        return await self._manager.requeue(job_id)
