"""应用入口（设计说明书 §37 完整架构）。

启动：
    uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from .api import dlq_router, jobs_router
from .config import QueueConfig
from .factory import Runtime, build_runtime

logger = logging.getLogger(__name__)


def create_app(
    config: QueueConfig | None = None,
    runtime: Runtime | None = None,
) -> FastAPI:
    """组装 FastAPI 应用。测试可通过注入 ``runtime`` 复用组件。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = getattr(app.state, "runtime", None)
        if rt is None:
            rt = runtime if runtime is not None else await build_runtime(config)
            app.state.runtime = rt
        await rt.start()  # 启动 Worker Pool + Reaper
        logger.info("app started: backend=%s queue=%s workers=%d",
                    rt.config.storage_backend, rt.config.queue_backend,
                    rt.config.worker_count)
        yield
        await rt.stop()

    app = FastAPI(
        title="Async Agent Job Queue",
        version="0.1.0",
        description="把长时间运行的 Agent 从 HTTP 请求生命周期解耦成持久化 Job "
                    "(Job/Queue/Worker/Checkpoint/Retry/Lease/DLQ/Event Store)",
        lifespan=lifespan,
    )
    # 注入 runtime 时（如测试），直接挂到 state，避免依赖 lifespan 触发
    if runtime is not None:
        app.state.runtime = runtime
    app.include_router(jobs_router)
    app.include_router(dlq_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> PlainTextResponse:
        rt: Runtime = request.app.state.runtime
        # 实时刷新两个 gauge（§46）
        rt.metrics.set_gauge("agent_queue_depth", rt.queue.depth())
        rt.metrics.set_gauge("agent_worker_active", rt.pool.active_count if rt.pool else 0)
        return PlainTextResponse(rt.metrics.render_prometheus())

    return app


app = create_app()
