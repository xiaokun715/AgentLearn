"""Worker / WorkerPool / Queue（设计说明书 §11-14 / §45）。"""
from __future__ import annotations

import pytest

from app.domain.status import JobStatus
from app.queue.memory import FairMemoryQueue, MemoryQueue

from .conftest import make_memory_config, wait_terminal


async def test_single_job_lifecycle(runtime):
    """queued -> running -> success，progress 到 100，current_step 到最后一个。"""
    job = await runtime.job_service.create(
        agent="research_agent", input={"query": "NVIDIA 财报"}
    )
    assert job.status == JobStatus.QUEUED

    final = await wait_terminal(runtime, job.id)
    assert final.status == JobStatus.SUCCESS
    assert final.progress == 100
    assert final.current_step == "generate_report"
    assert final.result is not None and "report" in final.result
    assert final.finished_at is not None


async def test_concurrency_20_jobs_3_workers(make_runtime):
    """§45：100 jobs / 10 workers 的前置演练——20 jobs / 3 workers 全成功。"""
    import asyncio

    rt = await make_runtime(worker_count=3)
    try:
        jobs = [
            await rt.job_service.create(
                agent="research_agent", input={"query": f"查询 {i}"}
            )
            for i in range(20)
        ]
        results = await asyncio.gather(
            *[wait_terminal(rt, j.id) for j in jobs]
        )
        assert all(r.status == JobStatus.SUCCESS for r in results)
        counts = await rt.job_store.count_by_status()
        assert counts.get("success") == 20
    finally:
        await rt.stop()


async def test_worker_pool_active_gauge(runtime):
    job = await runtime.job_service.create(
        agent="research_agent", input={"query": "q"}
    )
    await wait_terminal(runtime, job.id)
    assert runtime.pool.active_count == 0  # 任务都跑完了
    assert runtime.pool.worker_ids == ["worker-1", "worker-2"]


# ---- 队列机制 -----------------------------------------------------------


async def test_memory_queue_priority_order():
    q = MemoryQueue()
    await q.publish("low", priority=5)
    await q.publish("high", priority=1)
    await q.publish("mid", priority=3)
    assert await q.get() == "high"
    assert await q.get() == "mid"
    assert await q.get() == "low"
    assert q.depth() == 0


async def test_memory_queue_fifo_within_same_priority():
    q = MemoryQueue()
    await q.publish("a", priority=0)
    await q.publish("b", priority=0)
    assert await q.get() == "a"
    assert await q.get() == "b"


async def test_fair_queue_round_robin_across_tenants():
    """§49：Fair Scheduling —— Tenant A 塞满也不会饿死 Tenant B。"""
    q = FairMemoryQueue()
    await q.publish("a1", priority=0, tenant="A")
    await q.publish("a2", priority=0, tenant="A")
    await q.publish("b1", priority=0, tenant="B")

    got = [await q.get(), await q.get(), await q.get()]
    # Round Robin：B 至少一次在 A 的第二次之前出现
    assert got.count("b1") == 1
    assert "a1" in got and "a2" in got
    # B 不会排在最后（若纯 FIFO 全局队列，B 必然最后）
    assert got.index("b1") < 2


async def test_metrics_recorded_on_success(runtime):
    job = await runtime.job_service.create(
        agent="research_agent", input={"query": "q"}
    )
    await wait_terminal(runtime, job.id)
    snap = runtime.metrics.snapshot()
    assert snap["counters"]["agent_jobs_created_total"] == 1
    assert snap["counters"]["agent_jobs_completed_total"] == 1
    for name in ("agent_job_duration_seconds",
                 "agent_job_queue_wait_seconds",
                 "agent_job_execution_seconds"):
        h = snap["histograms"][name]
        assert h["count"] >= 1, f"{name} 应至少记录一个样本（§46-47）"
