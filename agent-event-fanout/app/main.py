"""应用入口（设计说明书 §31~§32, §39）。

启动：
    uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import deliveries, events, subscribers
from .config import EventFanoutConfig
from .domain.exceptions import EventFanoutError, NotFoundError, ConflictError
from .factory import Runtime, build_runtime

logger = logging.getLogger(__name__)


def _status_for(exc: Exception) -> int:
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ConflictError):
        return 409
    if isinstance(exc, EventFanoutError):
        return 400
    if isinstance(exc, ValueError):
        return 400
    return 500


def create_app(
    config: EventFanoutConfig | None = None,
    runtime: Runtime | None = None,
) -> FastAPI:
    """组装 FastAPI 应用。测试可通过注入 ``runtime`` 复用组件。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = getattr(app.state, "runtime", None)
        if rt is None:
            rt = runtime if runtime is not None else await build_runtime(config)
            app.state.runtime = rt
        await rt.start()
        logger.info(
            "app started: storage=%s queue=%s",
            rt.config.storage_backend,
            rt.config.queue_backend,
        )
        yield
        await rt.stop()

    app = FastAPI(
        title="Agent Event Fan-out",
        version="0.1.0",
        description="Webhook 与事件分发系统 —— Event Store / Outbox / Fan-out / "
                    "Delivery 状态机 / Retry / DLQ / HMAC 签名 / 幂等",
        lifespan=lifespan,
    )
    if runtime is not None:
        app.state.runtime = runtime

    app.include_router(subscribers.router)
    app.include_router(events.router)
    app.include_router(deliveries.router)

    @app.exception_handler(EventFanoutError)
    async def domain_error_handler(request: Request, exc: EventFanoutError):
        return JSONResponse(
            status_code=_status_for(exc),
            content={"detail": str(exc), "type": type(exc).__name__},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> dict:
        rt: Runtime = request.app.state.runtime
        return {
            "status": "ok",
            "storage": rt.config.storage_backend,
            "queue": rt.config.queue_backend,
            "pending_outbox": await rt.repo.count_outbox_pending(),
        }

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        """Prometheus 文本格式指标（§40）。"""
        from fastapi.responses import PlainTextResponse

        rt: Runtime = request.app.state.runtime
        return PlainTextResponse(rt.metrics.render())

    return app


app = create_app()
