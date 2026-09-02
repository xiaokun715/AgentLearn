"""应用入口（设计说明书 §30~§32）。

启动：
    uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .api import security
from .config import GuardrailsConfig
from .core.exceptions import (
    GuardrailsError,
    InvalidApprovalError,
    NotFoundError,
)
from .factory import build_guardrails
from .service import Guardrails

logger = logging.getLogger(__name__)


def _status_for(exc: Exception) -> int:
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, InvalidApprovalError):
        return 409
    if isinstance(exc, GuardrailsError):
        return 400
    if isinstance(exc, ValueError):
        return 400
    return 500


def create_app(
    config: GuardrailsConfig | None = None,
    guardrails: Guardrails | None = None,
) -> FastAPI:
    """组装 FastAPI 应用。测试可注入 ``guardrails`` 复用同一实例。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        g = getattr(app.state, "guardrails", None)
        if g is None:
            g = guardrails if guardrails is not None else build_guardrails(config)
            app.state.guardrails = g
        logger.info(
            "guardrails started: config_dir=%s tools=%d",
            g.config.config_dir, len(g.tool_registry.names()),
        )
        yield

    app = FastAPI(
        title="Guardrails Middleware",
        version="0.1.0",
        description="AI Security Middleware —— Input / Context / Tool / Tool Result / "
                    "Output 统一安全边界：Detect + Policy + Action + Audit + Metrics",
        lifespan=lifespan,
    )
    if guardrails is not None:
        app.state.guardrails = guardrails

    app.include_router(security.router)

    @app.exception_handler(GuardrailsError)
    async def domain_error_handler(request: Request, exc: GuardrailsError):
        return JSONResponse(
            status_code=_status_for(exc),
            content={"detail": str(exc), "type": type(exc).__name__},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> dict:
        g: Guardrails = request.app.state.guardrails
        return {
            "status": "ok",
            "tools": len(g.tool_registry.names()),
            "audit_events": g.audit.count(),
            "pending_approvals": len(g.list_approvals("PENDING")),
        }

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        g: Guardrails = request.app.state.guardrails
        return PlainTextResponse(g.metrics.render())

    return app


app = create_app()
