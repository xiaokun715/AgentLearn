"""Checkpoint（设计说明书 §17-21）。"""
from __future__ import annotations

import pytest

from app.agent.state import AgentState
from app.domain.job import Job

from .conftest import wait_terminal


async def test_checkpoint_store_roundtrip():
    from app.checkpoint.memory import MemoryCheckpointStore

    store = MemoryCheckpointStore()
    cp = {"job_id": "job_1", "step": "search", "completed_steps": ["analyze"]}
    await store.save("job_1", cp)
    assert await store.load("job_1") == cp
    await store.delete("job_1")
    assert await store.load("job_1") is None


async def test_checkpoint_deleted_on_success(runtime):
    job = await runtime.job_service.create(
        agent="research_agent", input={"query": "q"}
    )
    await wait_terminal(runtime, job.id)
    assert await runtime.checkpoint_store.load(job.id) is None  # 完成后清理


async def test_checkpoint_persists_completed_steps(make_runtime):
    """崩溃在 search 的 tool 成功后 -> Checkpoint 里应有 analyze 完成 + tool 成功记录。

    关闭自动 Reaper，让 Job 停留在「崩溃但未恢复」状态以便检查 Checkpoint。
    """
    rt = await make_runtime(worker_count=1, start_reaper=False)
    try:
        job = await rt.job_service.create(
            agent="chaos_agent",
            input={
                "query": "q",
                "chaos": {"crash_after_tool": True, "llm_latency": 0.01, "tool_latency": 0.01},
            },
        )
        # 等它先跑起来、完成 analyze 并进入 search 的崩溃点
        import asyncio
        import time

        deadline = time.monotonic() + 10
        cp = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.02)
            cp = await rt.checkpoint_store.load(job.id)
            # 崩溃点后、恢复前：tool_records 里应已有 success 记录
            # （write-ahead 的 running 记录不算，须等 tool 真正成功落盘）
            if (
                cp
                and "search" not in cp["completed_steps"]
                and any(
                    r.get("status") == "success"
                    for r in cp.get("tool_records", {}).values()
                )
            ):
                break
        assert cp is not None, "应有 checkpoint"
        assert "analyze" in cp["completed_steps"]
        assert "search" not in cp["completed_steps"]  # 该步尚未 checkpoint
        recs = cp["tool_records"]
        assert recs, "应有 tool 执行记录"
        assert all(r["status"] == "success" for r in recs.values())
    finally:
        await rt.stop()


async def test_agent_state_checkpoint_roundtrip():
    job = Job.create(agent_name="research_agent", input={"query": "NVIDIA"})
    st = AgentState.initial(job)
    st.apply("analyze", {"analysis": "计划"}, ["analyze", "search"])
    st.apply("search", {"search_result": ["r1"]}, ["analyze", "search"])
    st.tool_records["search:abc"] = {"status": "success", "result": ["r1"]}

    cp = st.to_checkpoint()
    st2 = AgentState.from_checkpoint(job, cp)
    assert st2.completed_steps == ["analyze", "search"]
    assert st2.analysis == "计划"
    assert st2.search_result == ["r1"]
    assert st2.tool_records["search:abc"]["status"] == "success"


async def test_next_step_and_progress():
    job = Job.create(agent_name="research_agent", input={"query": "q"})
    steps = ["analyze", "search", "analyze_result", "generate_report"]
    st = AgentState.initial(job)
    assert st.next_step(steps) == "analyze"
    assert st.progress(steps) == 0
    st.apply("analyze", {"analysis": "a"}, steps)
    assert st.next_step(steps) == "search"
    assert st.progress(steps) == 25
    assert not st.finished
    for s in steps[1:]:
        st.apply(s, {("search_result" if s == "search" else "insights" if s == "analyze_result" else "report"): "x"}, steps)
    assert st.finished
    assert st.next_step(steps) is None
    assert st.result["report"] == "x"
