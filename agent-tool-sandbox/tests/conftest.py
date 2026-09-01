"""测试夹具：默认用「内存存储 + ProcessSandbox」，零依赖即可跑全套安全测试。"""
from __future__ import annotations

import pytest_asyncio

from app.config import AppConfig
from app.factory import Runtime, build_runtime


@pytest_asyncio.fixture
async def make_runtime():
    """工厂夹具：按需定制配置，返回一个已启动的 Runtime。"""
    runtimes: list[Runtime] = []

    async def _make(**overrides) -> Runtime:
        config = AppConfig(
            storage_backend="memory",
            sandbox_backend="process",
            audit_enabled=True,
            **overrides,
        )
        rt = await build_runtime(config)
        runtimes.append(rt)
        return rt

    yield _make

    for rt in runtimes:
        await rt.stop()


@pytest_asyncio.fixture
async def runtime(make_runtime) -> Runtime:
    return await make_runtime()


@pytest_asyncio.fixture
async def client(make_runtime):
    """通过 ASGI 客户端直接调 API（不启动真实端口）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    rt = await make_runtime()
    app = create_app(runtime=rt)
    # ASGITransport 不触发 lifespan，手动挂上 runtime
    app.state.runtime = rt
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
