"""pytest 共享 fixture：全部用内存后端 + 快速时延，速度快、互不干扰。"""
from __future__ import annotations

import asyncio

import pytest

from app.factory import build_runtime
from tests.util import make_config


@pytest.fixture
async def runtime():
    """默认 Runtime（fast 配置）；teardown 时掐断仍在运行的 Agent，避免残留任务。"""
    rt = build_runtime(make_config())
    yield rt
    for tid in rt.registry.running_task_ids():
        rt.service.cancel(tid, mode="force")
    await asyncio.sleep(0.05)


@pytest.fixture
def make_runtime():
    """按需构造 Runtime（覆盖时延/模式参数）。"""
    def _make(**overrides):
        return build_runtime(make_config(**overrides))
    return _make


@pytest.fixture
async def api(runtime):
    """基于内存 runtime 的 ASGI HTTP 客户端（跳过 lifespan，避免二次启动）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app(runtime=runtime)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client
