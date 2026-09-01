"""Dead Letter Queue（设计说明书 §26-28 / §43）。"""
from __future__ import annotations

import time

from app.domain.status import JobStatus

from .conftest import wait_terminal

PERMA_FAIL = {
    "query": "q",
    "chaos": {
        "fail_at": ["search"],
        "fail_attempts": {"search": 999},
        "fail_with": "retryable",
    },
}


async def test_dlq_after_max_retries(runtime):
    """§43：3 次尝试全失败 -> DEAD（进入 DLQ）。"""
    job = await runtime.job_service.create(agent="chaos_agent", input=PERMA_FAIL)
    final = await wait_terminal(runtime, job.id, timeout=15)
    assert final.status == JobStatus.DEAD
    assert final.retry_count == 3  # max_retries=3
    assert "max_retries_exceeded" in (final.error or "")
    metrics = runtime.metrics.snapshot()["counters"]
    assert metrics["agent_jobs_dead_total"] == 1
    assert metrics["agent_jobs_failed_total"] == 1


async def test_dlq_list_shows_entry(runtime):
    job = await runtime.job_service.create(agent="chaos_agent", input=PERMA_FAIL)
    await wait_terminal(runtime, job.id, timeout=15)
    entries = await runtime.dlq_service.list()
    assert any(e["job_id"] == job.id for e in entries)
    entry = next(e for e in entries if e["job_id"] == job.id)
    assert entry["retry_count"] == 3
    assert entry["reason"] == "max_retries_exceeded"
    assert entry["last_error"]


async def test_dlq_requeue_reenters_queue(runtime):
    job = await runtime.job_service.create(agent="chaos_agent", input=PERMA_FAIL)
    await wait_terminal(runtime, job.id, timeout=15)
    assert (await runtime.job_service.get(job.id)).status == JobStatus.DEAD

    ok = await runtime.dlq_service.retry(job.id)
    assert ok is True
    queued = await runtime.job_service.get(job.id)
    assert queued.status == JobStatus.QUEUED
    assert queued.retry_count == 0  # 重置
    assert queued.error is None

    events = await runtime.job_service.events(job.id)
    assert "JOB_REQUEUED" in [e.event_type for e in events]

    # 重新入队后会再次执行并再次失败 -> 回到 DEAD（验证确实被消费了）
    final = await wait_terminal(runtime, job.id, timeout=15)
    assert final.status == JobStatus.DEAD


async def test_requeue_non_dead_returns_false(runtime):
    job = await runtime.job_service.create(
        agent="research_agent", input={"query": "q"}
    )
    await wait_terminal(runtime, job.id)
    assert await runtime.dlq_service.retry(job.id) is False


async def test_dlq_requeue_missing_job_returns_false(runtime):
    assert await runtime.dlq_service.retry("job_nope") is False
