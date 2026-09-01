"""Retry / Backoff（设计说明书 §24-26 / §42）。"""
from __future__ import annotations

from app.domain.status import JobStatus
from app.retry.policy import compute_backoff

from .conftest import wait_terminal

CHAOS = {
    "query": "q",
    "chaos": {
        "fail_at": ["search"],
        "fail_attempts": {"search": 2},
        "fail_with": "retryable",
        "llm_latency": 0.01,
        "tool_latency": 0.01,
    },
}


async def test_retry_succeeds_on_third_attempt(runtime):
    """§42：第一次 FAIL、第二次 FAIL、第三次 SUCCESS -> retry_count=2。"""
    job = await runtime.job_service.create(agent="chaos_agent", input=CHAOS)
    final = await wait_terminal(runtime, job.id)
    assert final.status == JobStatus.SUCCESS
    assert final.retry_count == 2
    assert runtime.metrics.snapshot()["counters"]["agent_jobs_retried_total"] == 2


async def test_retry_backoff_delays_are_exponential(runtime):
    """Backoff 延迟应为 base * 2^0, base * 2^1（默认 jitter=0，base=0.05）。"""
    job = await runtime.job_service.create(agent="chaos_agent", input=CHAOS)
    await wait_terminal(runtime, job.id)
    events = await runtime.job_service.events(job.id)
    retrying = [e for e in events if e.event_type == "JOB_RETRYING"]
    assert len(retrying) == 2
    delays = [e.payload["delay"] for e in retrying]
    assert delays[0] == pytest_approx(0.05)
    assert delays[1] == pytest_approx(0.10)


def pytest_approx(value, places=2):
    import pytest

    return pytest.approx(value, abs=10 ** (-places))


async def test_non_retryable_fails_immediately(runtime):
    """§24：NonRetryable -> 不重试，直接 FAILED，retry_count 保持 0。"""
    job = await runtime.job_service.create(
        agent="chaos_agent",
        input={
            "query": "q",
            "chaos": {
                "fail_at": ["analyze"],
                "fail_attempts": {"analyze": 1},
                "fail_with": "non_retryable",
            },
        },
    )
    final = await wait_terminal(runtime, job.id)
    assert final.status == JobStatus.FAILED
    assert final.retry_count == 0
    metrics = runtime.metrics.snapshot()["counters"]
    assert metrics["agent_jobs_failed_total"] == 1
    assert metrics["agent_jobs_retried_total"] == 0


async def test_exponential_backoff_computation():
    """§25：delay = base * 2**retry_count，加 jitter 防 Retry Storm。"""
    assert compute_backoff(0, base=1.0, jitter=0) == 1.0
    assert compute_backoff(1, base=1.0, jitter=0) == 2.0
    assert compute_backoff(2, base=1.0, jitter=0) == 4.0
    assert compute_backoff(3, base=1.0, jitter=0) == 8.0
    # max_delay 上限
    assert compute_backoff(10, base=1.0, max_delay=30.0, jitter=0) == 30.0
    # jitter 范围
    import random

    random.seed(42)
    for _ in range(20):
        d = compute_backoff(2, base=1.0, jitter=0.5)
        assert 4.0 <= d <= 4.5
