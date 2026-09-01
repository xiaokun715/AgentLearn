"""Tool 幂等（设计说明书 §22-23 / §40）。"""
from __future__ import annotations

import asyncio
import time

from app.agent.tools import SearchWebTool, make_tool_call_id
from app.domain.status import JobStatus

from .conftest import wait_terminal


async def test_tool_call_id_deterministic():
    args = {"query": "NVIDIA", "top_k": 3}
    assert make_tool_call_id("search_web", args) == make_tool_call_id("search_web", args)
    assert make_tool_call_id("search_web", {"query": "NVIDIA", "top_k": 3}) == \
           make_tool_call_id("search_web", {"top_k": 3, "query": "NVIDIA"})  # 键序无关
    assert make_tool_call_id("search_web", {"query": "NVIDIA", "top_k": 3}) != \
           make_tool_call_id("search_web", {"query": "AMD", "top_k": 3})


async def test_tool_roundtrip():
    tool = SearchWebTool(latency=0.0, top_k=2)
    r = await tool.run(query="q", top_k=2)
    assert len(r) == 2


async def test_tool_not_reexecuted_after_resume(make_runtime):
    """核心：Tool 成功 -> Checkpoint -> Worker 崩溃 -> 恢复后 Tool 不再执行（§23/§40）。"""
    rt = await make_runtime(worker_count=1, start_reaper=False)
    try:
        job = await rt.job_service.create(
            agent="chaos_agent",
            input={
                "query": "NVIDIA",
                "chaos": {"crash_after_tool": True, "llm_latency": 0.01, "tool_latency": 0.01},
            },
        )
        # 等崩溃（租约过期）
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            j = await rt.job_service.get(job.id)
            if j.status == JobStatus.RUNNING and j.lease_expire_at and j.lease_expire_at < time.time():
                break
            await asyncio.sleep(0.02)
        # 崩溃前 Checkpoint 里应有 tool 的 success 记录
        cp = await rt.checkpoint_store.load(job.id)
        assert cp and any(r["status"] == "success" for r in cp["tool_records"].values())

        await rt.pool.reaper.reap_once()
        final = await wait_terminal(rt, job.id)
        assert final.status == JobStatus.SUCCESS

        events = await rt.job_service.events(job.id)
        types = [e.event_type for e in events]
        # Tool 只真正执行了一次；恢复时被幂等跳过
        assert types.count("TOOL_CALLED") == 1
        assert types.count("TOOL_SKIPPED") == 1
        assert types.count("TOOL_COMPLETED") == 1  # 只有真正执行的那次
    finally:
        await rt.stop()


async def test_duplicate_publish_does_not_double_execute(make_runtime):
    """并发/重复投递：即使两个 Worker 同时取到同一 job，也只有一个会执行（Lease 兜底）。"""
    rt = await make_runtime(worker_count=2)
    try:
        job = await rt.job_service.create(
            agent="research_agent", input={"query": "q"}
        )
        # 模拟重复投递：同一个 job 再入队两次
        for _ in range(2):
            await rt.queue.publish(job.id, priority=job.priority, tenant=job.tenant_id)

        final = await wait_terminal(rt, job.id)
        assert final.status == JobStatus.SUCCESS
        events = await rt.job_service.events(job.id)
        # JOB_STARTED / JOB_COMPLETED 只会出现一次（Lease 兜底防重复执行）
        started = [e for e in events if e.event_type == "JOB_STARTED"]
        assert len(started) == 1
        completed = [e for e in events if e.event_type == "JOB_COMPLETED"]
        assert len(completed) == 1
    finally:
        await rt.stop()
