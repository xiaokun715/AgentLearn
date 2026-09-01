"""JobExecutor —— 任务生命周期（设计说明书 §15 / §39）。

Worker 只负责「消费任务」，JobExecutor 负责「任务生命周期」：

    load_job -> acquire_lease -> resume_checkpoint -> execute_agent
        -> checkpoint -> retry / finalize

并处理三件最核心的事：
1. Lease + Heartbeat：Worker 崩溃后由 Reaper 依据租约接管（§29-31）。
2. Checkpoint：每个 step 完成后保存安全恢复点（§17-21）。
3. 幂等 Tool：Tool Execution Record + write-ahead（§22-23 / §40）。
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..agent.context import StepContext
from ..agent.llm import MockLLM
from ..agent.registry import AgentRegistry
from ..agent.state import AgentState
from ..checkpoint.base import CheckpointStore
from ..config import QueueConfig
from ..dlq.manager import DlqManager
from ..domain.events import JobEventType
from ..domain.exceptions import (
    CancellationRequested,
    RetryableError,
    WorkerCrash,
)
from ..domain.job import Job
from ..domain.status import JobStatus
from ..observability.metrics import Metrics
from ..queue.base import JobQueue
from ..retry.policy import RetryPolicy, compute_backoff
from ..storage.event_store import EventStore
from ..storage.job_store import JobStore

logger = logging.getLogger(__name__)


class AbortGate:
    """一次执行内的中止信号：取消 / 租约丢失。"""

    def __init__(self) -> None:
        self.cancel = asyncio.Event()
        self.lease_lost = asyncio.Event()

    def raise_if_aborted(self) -> None:
        if self.cancel.is_set():
            raise CancellationRequested("job cancellation requested")
        if self.lease_lost.is_set():
            raise WorkerCrash("lease lost to another worker")


class JobExecutor:
    def __init__(
        self,
        *,
        config: QueueConfig,
        job_store: JobStore,
        event_store: EventStore,
        checkpoint_store: CheckpointStore,
        queue: JobQueue,
        agent_registry: AgentRegistry,
        dlq_manager: DlqManager,
        retry_policy: RetryPolicy | None = None,
        llm: MockLLM | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.config = config
        self.job_store = job_store
        self.event_store = event_store
        self.checkpoint_store = checkpoint_store
        self.queue = queue
        self.agent_registry = agent_registry
        self.dlq_manager = dlq_manager
        self.retry_policy = retry_policy or RetryPolicy()
        self.llm = llm or MockLLM()
        self.metrics = metrics or Metrics()
        # 后台的延迟重投任务（_delayed_requeue），stop 时统一取消，避免泄漏
        self._background_tasks: set[asyncio.Task] = set()

    # ---- 主入口 -------------------------------------------------------------

    async def execute(self, worker_id: str, job_id: str) -> None:
        job = await self.job_store.get(job_id)
        if job is None:
            return
        # 已被取消 / 不是可执行状态 -> 跳过（queue 里可能残留已取消的 job）
        if job.status == JobStatus.CANCELLED or job.status not in (
            JobStatus.QUEUED, JobStatus.RETRYING,
        ):
            return

        # 1) 原子获取租约（§29-31）；拿不到说明别人正在处理
        if not await self.job_store.acquire_lease(job_id, worker_id, self.config.lease_duration):
            return

        gate = AbortGate()
        heartbeat = asyncio.create_task(self._heartbeat(job_id, worker_id, gate))
        cancel_watcher = asyncio.create_task(self._watch_cancel(job_id, gate))

        try:
            await self._run(job_id, worker_id, gate)
        except CancellationRequested:
            await self._finalize_cancel(job_id, worker_id)
        except WorkerCrash as e:
            # §41：Worker 崩溃 -> 不判失败，等 Lease 过期后由 Reaper 接管
            logger.warning("job %s worker %s crashed: %s", job_id, worker_id, e)
            await self.job_store.expire_lease(job_id, worker_id)
        except RetryableError as e:
            fresh = await self.job_store.get(job_id)
            if fresh is not None:
                await self._handle_retry(fresh, e)
        except Exception as e:  # noqa: BLE001
            fresh = await self.job_store.get(job_id)
            if fresh is not None:
                await self._handle_failure(fresh, e)
        finally:
            heartbeat.cancel()
            cancel_watcher.cancel()
            await asyncio.gather(heartbeat, cancel_watcher, return_exceptions=True)

    async def _run(self, job_id: str, worker_id: str, gate: AbortGate) -> None:
        # 2) 标记 RUNNING + 首次启动时间（queue_wait 指标只在第一次计算，§47）
        job = await self.job_store.get(job_id)
        assert job is not None
        first_start = job.started_at is None
        started = time.time() if first_start else job.started_at
        ok = await self.job_store.transition(
            job_id, job.status, JobStatus.RUNNING,
            worker_id=worker_id, started_at=started, error=None,
        )
        if not ok:
            await self.job_store.expire_lease(job_id, worker_id)
            return

        job = await self.job_store.get(job_id)
        assert job is not None
        await self.emit_event(job_id, JobEventType.JOB_STARTED.value,
                              {"worker_id": worker_id, "attempt": job.retry_count + 1})
        if first_start and job.queued_at is not None:
            self.metrics.observe(
                "agent_job_queue_wait_seconds", max(0.0, started - job.queued_at)
            )

        # 3) 恢复 Checkpoint（§31 断点续跑）
        agent = self.agent_registry.get(job.agent_name)
        cp = await self.checkpoint_store.load(job_id)
        state = AgentState.from_checkpoint(job, cp)
        ctx = StepContext(executor=self, agent=agent, job=job, state=state, gate=gate)
        exec_start = time.monotonic()

        # 4) 逐步执行 + 逐步 Checkpoint（§20）
        while True:
            gate.raise_if_aborted()
            step = state.next_step(agent.steps)
            if step is None:
                break
            ctx.step = step
            await self.emit_event(job_id, JobEventType.STEP_STARTED.value, {"step": step})
            await self.job_store.update_progress(
                job_id, worker_id, step=step, progress=state.progress(agent.steps)
            )
            try:
                result = await agent.execute_step(step, state, ctx)
            except Exception as e:
                await self.emit_event(job_id, JobEventType.STEP_FAILED.value,
                                      {"step": step, "error": str(e)})
                raise
            state.apply(step, result, agent.steps)
            await self.save_checkpoint(state, reason="step")
            await self.emit_event(job_id, JobEventType.STEP_COMPLETED.value, {"step": step})
            await self.emit_event(job_id, JobEventType.CHECKPOINT_SAVED.value, {"step": step})
            await self.job_store.update_progress(
                job_id, worker_id, step=step, progress=state.progress(agent.steps)
            )

        # 5) 成功收尾
        await self.job_store.transition(
            job_id, JobStatus.RUNNING, JobStatus.SUCCESS,
            result=state.result, finished_at=time.time(),
            worker_id=None, lease_expire_at=None, progress=100,
            cancel_requested=False, error=None,
        )
        await self.emit_event(job_id, JobEventType.JOB_COMPLETED.value, {"result": state.result})
        await self.checkpoint_store.delete(job_id)
        self.metrics.inc("agent_jobs_completed_total")
        if job.created_at:
            self.metrics.observe("agent_job_duration_seconds", time.time() - job.created_at)
        self.metrics.observe("agent_job_execution_seconds", time.monotonic() - exec_start)
        logger.info("job %s completed by worker %s", job_id, worker_id)

    # ---- 供 StepContext 调用的公开方法 ----------------------------------------

    async def close(self) -> None:
        """取消所有尚未触发的延迟重投任务（进程退出时调用）。"""
        tasks = list(self._background_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def emit_event(self, job_id: str, event_type: str, payload: dict | None = None) -> None:
        await self.event_store.append(job_id, event_type, payload)

    async def save_checkpoint(self, state: AgentState, reason: str = "step") -> None:
        try:
            await self.checkpoint_store.save(state.job_id, state.to_checkpoint())
            self.metrics.inc("agent_checkpoint_total")
        except Exception:
            self.metrics.inc("agent_checkpoint_failure_total")
            raise

    # ---- 异常处理（§24 / §26-27） ---------------------------------------------

    async def _handle_retry(self, job: Job, error: BaseException) -> None:
        if not self.retry_policy.should_retry(error):
            await self._handle_failure(job, error)
            return
        new_retry = job.retry_count + 1
        if new_retry < job.max_retries:
            delay = compute_backoff(
                job.retry_count,
                base=self.config.backoff_base,
                max_delay=self.config.backoff_max,
                jitter=self.config.backoff_jitter,
            )
            ok = await self.job_store.transition(
                job.id, JobStatus.RUNNING, JobStatus.RETRYING,
                retry_count=new_retry, error=str(error),
                worker_id=None,
                # 租约覆盖整个 backoff 窗口，避免 Reaper 在等待期内误回收；
                # 若进程在 backoff 期间崩溃，租约到期后 Reaper 仍能接管。
                lease_expire_at=time.time() + max(self.config.lease_duration, delay),
            )
            if not ok:
                return
            await self.emit_event(job.id, JobEventType.JOB_RETRYING.value,
                                  {"attempt": new_retry, "delay": round(delay, 2),
                                   "error": str(error)})
            self.metrics.inc("agent_jobs_retried_total")
            task = asyncio.create_task(self._delayed_requeue(job.id, delay))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            logger.warning("job %s retry %d/%d in %.2fs",
                           job.id, new_retry, job.max_retries, delay)
        else:
            # 重试次数用尽 -> FAILED -> DLQ（§26-27）
            await self.job_store.transition(
                job.id, JobStatus.RUNNING, JobStatus.FAILED,
                retry_count=new_retry, error=str(error),
                worker_id=None, lease_expire_at=None, finished_at=time.time(),
            )
            await self.emit_event(job.id, JobEventType.JOB_FAILED.value,
                                  {"attempt": new_retry, "error": str(error)})
            self.metrics.inc("agent_jobs_failed_total")
            await self.dlq_manager.send(
                job.id, reason="max_retries_exceeded", last_error=str(error)
            )

    async def _handle_failure(self, job: Job, error: BaseException) -> None:
        """不可重试 / 未知异常 -> FAILED（§24：不 Retry）。"""
        await self.job_store.transition(
            job.id, JobStatus.RUNNING, JobStatus.FAILED,
            error=str(error), worker_id=None, lease_expire_at=None,
            finished_at=time.time(),
        )
        await self.emit_event(job.id, JobEventType.JOB_FAILED.value, {"error": str(error)})
        self.metrics.inc("agent_jobs_failed_total")
        logger.error("job %s failed: %s", job.id, error)

    async def _finalize_cancel(self, job_id: str, worker_id: str) -> None:
        """§44：Agent 收到 cancellation -> 停止 -> Checkpoint -> CANCELLED。"""
        try:
            job = await self.job_store.get(job_id)
            if job is not None:
                cp = await self.checkpoint_store.load(job_id)
                agent = self.agent_registry.get(job.agent_name)
                state = AgentState.from_checkpoint(job, cp)
                await self.save_checkpoint(state, reason="cancel")
        except Exception:  # noqa: BLE001
            pass
        await self.emit_event(job_id, JobEventType.JOB_CANCELLED.value, {"worker_id": worker_id})
        await self.job_store.transition(
            job_id, JobStatus.RUNNING, JobStatus.CANCELLED,
            worker_id=None, lease_expire_at=None, cancel_requested=False,
            finished_at=time.time(), error="cancelled",
        )
        self.metrics.inc("agent_jobs_cancelled_total")
        logger.info("job %s cancelled by worker %s", job_id, worker_id)

    # ---- 后台任务 -------------------------------------------------------------

    async def _heartbeat(self, job_id: str, worker_id: str, gate: AbortGate) -> None:
        """每 heartbeat_interval 刷新租约；续约失败说明被接管，立刻中止。"""
        try:
            while True:
                await asyncio.sleep(self.config.heartbeat_interval)
                ok = await self.job_store.renew_lease(
                    job_id, worker_id, self.config.lease_duration
                )
                if not ok:
                    gate.lease_lost.set()
                    return
        except asyncio.CancelledError:
            pass

    async def _watch_cancel(self, job_id: str, gate: AbortGate) -> None:
        """轮询 cancel_requested 标志，置位则触发协作式取消（§10）。"""
        try:
            while True:
                if await self.job_store.is_cancel_requested(job_id):
                    gate.cancel.set()
                    return
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    async def _delayed_requeue(self, job_id: str, delay: float) -> None:
        """Backoff 结束后把 RETRYING 重新入队（§25 / §42）。"""
        await asyncio.sleep(delay)
        job = await self.job_store.get(job_id)
        if job is None:
            return
        if await self.job_store.is_cancel_requested(job_id):
            await self.job_store.transition(
                job_id, JobStatus.RETRYING, JobStatus.CANCELLED,
                worker_id=None, lease_expire_at=None, cancel_requested=False,
                finished_at=time.time(), error="cancelled",
            )
            await self.emit_event(job_id, JobEventType.JOB_CANCELLED.value,
                                  {"reason": "cancelled_while_retrying"})
            self.metrics.inc("agent_jobs_cancelled_total")
            return
        ok = await self.job_store.transition(
            job_id, JobStatus.RETRYING, JobStatus.QUEUED,
            queued_at=time.time(), worker_id=None, lease_expire_at=None,
            cancel_requested=False,
        )
        if not ok:
            return
        fresh = await self.job_store.get(job_id)
        assert fresh is not None
        await self.emit_event(job_id, JobEventType.JOB_REQUEUED.value, {"after_seconds": delay})
        await self.queue.publish(job_id, priority=fresh.priority, tenant=fresh.tenant_id)
