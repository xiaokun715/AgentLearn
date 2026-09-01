"""故障恢复（设计说明书 §29-31 / §41 / §55 问题3）。

核心验证：Worker Crash -> Lease 过期 -> Reaper 重新入队
            -> 其他 Worker 从 Checkpoint 断点续跑 -> SUCCESS。
"""
from __future__ import annotations

import asyncio
import time

from app.domain.status import JobStatus

from .conftest import wait_terminal

CRASH_CFG = {
    "query": "q",
    "chaos": {"crash_after_tool": True, "llm_latency": 0.01, "tool_latency": 0.01},
}


async def test_reaper_finds_expired_lease(make_runtime):
    """Lease 过期后 Reaper 应把 RUNNING 的 Job 重新入队。"""
    rt = await make_runtime(worker_count=1, start_reaper=False)
    try:
        job = await rt.job_service.create(agent="chaos_agent", input=CRASH_CFG)
        # 等它跑起来（进入 RUNNING）
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            j = await rt.job_service.get(job.id)
            if j.status == JobStatus.RUNNING:
                break
            await asyncio.sleep(0.02)
        running = await rt.job_service.get(job.id)
        assert running.status == JobStatus.RUNNING

        # 模拟 Worker 崩溃：租约立即过期
        await rt.job_store.expire_lease(job.id, running.worker_id)
        recoverable = await rt.job_store.find_recoverable(time.time(), 0.0)
        assert any(j.id == job.id for j in recoverable)

        # Reaper 手动触发一轮
        assert rt.pool.reaper is not None
        recovered = await rt.pool.reaper.reap_once()
        assert recovered >= 1
        j = await rt.job_service.get(job.id)
        assert j.status == JobStatus.QUEUED
        assert j.worker_id is None
    finally:
        await rt.stop()


async def test_crash_resume_reaches_success(make_runtime):
    """端到端：崩溃 -> 恢复 -> 断点续跑 -> SUCCESS（§41）。"""
    rt = await make_runtime(worker_count=2, start_reaper=False)
    try:
        job = await rt.job_service.create(agent="chaos_agent", input=CRASH_CFG)
        # 等它进入 RUNNING 且崩溃（租约过期）
        deadline = time.monotonic() + 10
        crashed = False
        while time.monotonic() < deadline:
            j = await rt.job_service.get(job.id)
            if j.status == JobStatus.RUNNING and j.lease_expire_at and j.lease_expire_at < time.time():
                crashed = True
                break
            await asyncio.sleep(0.02)
        assert crashed, "job 应处于崩溃后的 RUNNING（租约过期）状态"

        # 手动接管：reap -> 重新入队 -> worker 续跑
        recovered = await rt.pool.reaper.reap_once()
        assert recovered == 1
        final = await wait_terminal(rt, job.id)
        assert final.status == JobStatus.SUCCESS
        assert "report" in (final.result or {})
    finally:
        await rt.stop()


async def test_sqlite_backend_durable_recovery():
    """SQLite 持久化后端：崩溃恢复 + 重启后结果仍在（§13 V1 目标）。"""
    import tempfile

    from app.config import QueueConfig
    from app.factory import build_runtime

    tmp = tempfile.mkdtemp()
    cfg = QueueConfig(
        storage_backend="sqlite",
        database_url=f"sqlite:///{tmp}/jobs.db",
        queue_backend="memory",
        worker_count=1,
        lease_duration=0.3,
        heartbeat_interval=0.1,
        reaper_interval=0.3,
        reaper_grace=0.0,
        max_retries=3,
        backoff_base=0.05,
        backoff_jitter=0.0,
    )

    rt = await build_runtime(cfg, start_reaper=False)
    await rt.start()
    try:
        job = await rt.job_service.create(agent="chaos_agent", input=CRASH_CFG)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            j = await rt.job_service.get(job.id)
            if j.status == JobStatus.RUNNING and j.lease_expire_at and j.lease_expire_at < time.time():
                break
            await asyncio.sleep(0.02)
        await rt.pool.reaper.reap_once()
        await wait_terminal(rt, job.id)
        final = await rt.job_service.get(job.id)
        assert final.status == JobStatus.SUCCESS
    finally:
        await rt.stop()

    # 模拟进程重启：用同一个 DB 重新构造 runtime，Job 状态/结果仍在
    rt2 = await build_runtime(cfg)
    try:
        reloaded = await rt2.job_service.get(job.id)
        assert reloaded.status == JobStatus.SUCCESS
        assert reloaded.result is not None and "report" in reloaded.result
    finally:
        await rt2.stop()


async def test_recovery_does_not_restart_from_zero(make_runtime):
    """恢复后不从头重跑：analyze 只执行一次，Tool 只真正执行一次（幂等跳过）。"""
    rt = await make_runtime(worker_count=1, start_reaper=False)
    try:
        job = await rt.job_service.create(agent="chaos_agent", input=CRASH_CFG)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            j = await rt.job_service.get(job.id)
            if j.status == JobStatus.RUNNING and j.lease_expire_at and j.lease_expire_at < time.time():
                break
            await asyncio.sleep(0.02)
        await rt.pool.reaper.reap_once()
        await wait_terminal(rt, job.id)

        events = await rt.job_service.events(job.id)
        steps = [e.payload.get("step") for e in events if e.event_type == "STEP_STARTED"]
        # 崩溃前完成的 analyze 不会被重做；generate_report 只跑一次
        assert steps.count("analyze") == 1
        assert steps.count("generate_report") == 1
        # 恢复时 search 步骤会重新 START（第一次 START 后崩溃），
        # 但 Tool 只真正执行一次（§23/§40），其余命中幂等缓存
        types = [e.event_type for e in events]
        assert types.count("TOOL_CALLED") == 1
        assert types.count("TOOL_SKIPPED") == 1
    finally:
        await rt.stop()
