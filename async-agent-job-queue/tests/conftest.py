"""pytest 共享 fixture。

默认全部使用内存后端：memory storage + asyncio 内存队列，
速度快、隔离好；SQLite/PostgreSQL 的持久化与恢复能力在
test_recovery / test_checkpoint 里单独覆盖。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.config import QueueConfig
from app.factory import build_runtime


def make_memory_config(**overrides) -> QueueConfig:
    defaults = dict(
        storage_backend="memory",
        queue_backend="memory",
        worker_count=2,
        lease_duration=0.3,
        heartbeat_interval=0.1,
        reaper_interval=0.3,
        reaper_grace=0.0,
        max_retries=3,
        backoff_base=0.05,
        backoff_jitter=0.0,
    )
    defaults.update(overrides)
    return QueueConfig(**defaults)


async def wait_terminal(rt, job_id: str, timeout: float = 15.0, interval: float = 0.02):
    """轮询直到 Job 进入终态（success/failed/cancelled/dead），返回最终 Job。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = await rt.job_service.get(job_id)
        if job.status.value in ("success", "failed", "cancelled", "dead"):
            return job
        await asyncio.sleep(interval)
    return await rt.job_service.get(job_id)


@pytest.fixture
async def runtime():
    """默认运行环境：2 个 worker + 自动 Reaper，内存后端。"""
    rt = await build_runtime(make_memory_config())
    await rt.start()
    try:
        yield rt
    finally:
        await rt.stop()


@pytest.fixture
def make_runtime():
    """按需构造运行环境（例如关闭 Reaper 做确定性恢复测试）。"""

    async def _make(**overrides):
        start_reaper = overrides.pop("start_reaper", True)
        rt = await build_runtime(
            make_memory_config(**overrides), start_reaper=start_reaper
        )
        await rt.start()
        return rt

    return _make


@pytest.fixture
async def api(runtime):
    """基于内存 runtime 的 ASGI HTTP 客户端（不走 lifespan，避免二次启动）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app(runtime=runtime)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
