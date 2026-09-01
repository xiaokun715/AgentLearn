"""取消（设计说明书 §10 / §44）。"""
from __future__ import annotations

import asyncio
import time

from app.domain.status import JobStatus

from .conftest import wait_terminal


async def test_cancel_queued_job(runtime):
    job = await runtime.job_service.create(
        agent="research_agent", input={"query": "q"}
    )
    cancelled = await runtime.job_service.cancel(job.id)
    assert cancelled.status == JobStatus.CANCELLED

    final = await wait_terminal(runtime, job.id)
    assert final.status == JobStatus.CANCELLED
    # Worker 取到后看到 CANCELLED 直接跳过，不会执行
    events = await runtime.job_service.events(job.id)
    types = [e.event_type for e in events]
    assert "JOB_CANCELLED" in types
    assert "JOB_STARTED" not in types


async def test_cancel_running_job(make_runtime):
    """RUNNING 中取消：置位信号 -> Worker 在下一个边界协作停止 -> CANCELLED。"""
    rt = await make_runtime(worker_count=1)
    try:
        job = await rt.job_service.create(
            agent="chaos_agent",
            input={"query": "q", "chaos": {"llm_latency": 0.4, "tool_latency": 0.01}},
        )
        # 等它进入 RUNNING
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            j = await rt.job_service.get(job.id)
            if j.status == JobStatus.RUNNING:
                break
            await asyncio.sleep(0.02)
        assert (await rt.job_service.get(job.id)).status == JobStatus.RUNNING

        await rt.job_service.cancel(job.id)
        final = await wait_terminal(rt, job.id, timeout=10)
        assert final.status == JobStatus.CANCELLED
        events = await rt.job_service.events(job.id)
        types = [e.event_type for e in events]
        assert "JOB_CANCELLED" in types
        assert "JOB_COMPLETED" not in types
    finally:
        await rt.stop()


async def test_cancel_completed_job_is_noop(runtime):
    job = await runtime.job_service.create(
        agent="research_agent", input={"query": "q"}
    )
    await wait_terminal(runtime, job.id)
    after = await runtime.job_service.cancel(job.id)
    assert after.status == JobStatus.SUCCESS


async def test_cancel_retrying_job_does_not_requeue(runtime):
    """Backoff 期间取消：不重新入队，直接 CANCELLED。"""
    # 用较大的 backoff_base，让 job 在 RETRYING 停留足够久
    await runtime.stop()
    rt = await _build_slow_backoff()
    try:
        job = await rt.job_service.create(
            agent="chaos_agent",
            input={
                "query": "q",
                "chaos": {
                    "fail_at": ["search"],
                    "fail_attempts": {"search": 99},
                    "fail_with": "retryable",
                },
            },
        )
        # 等它进入 RETRYING
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            j = await rt.job_service.get(job.id)
            if j.status == JobStatus.RETRYING:
                break
            await asyncio.sleep(0.02)
        assert (await rt.job_service.get(job.id)).status == JobStatus.RETRYING

        await rt.job_service.cancel(job.id)
        final = await wait_terminal(rt, job.id, timeout=8)
        assert final.status == JobStatus.CANCELLED
    finally:
        await rt.stop()


async def _build_slow_backoff():
    from app.factory import build_runtime

    from .conftest import make_memory_config

    rt = await build_runtime(make_memory_config(backoff_base=2.0))
    await rt.start()
    return rt
