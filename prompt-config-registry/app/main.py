"""应用入口（设计说明书 §30 API 设计）。

启动：
    uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import configs, deployments, prompts, resolve
from .config import RegistryConfig
from .domain.exceptions import (
    ConfigRegistryError,
    ConflictError,
    DeploymentError,
    NotFoundError,
)
from .factory import Runtime, build_runtime

logger = logging.getLogger(__name__)


def _status_for(exc: Exception) -> int:
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ConflictError):
        return 409
    if isinstance(exc, DeploymentError):
        return 400
    if isinstance(exc, ConfigRegistryError):
        return 400
    if isinstance(exc, ValueError):
        return 400
    return 500


def create_app(
    config: RegistryConfig | None = None,
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
            "app started: storage=%s cache=%s",
            rt.config.storage_backend,
            rt.config.cache_backend,
        )
        yield
        await rt.stop()

    app = FastAPI(
        title="Prompt Config Registry",
        version="0.1.0",
        description="Prompt 与配置版本管理服务 —— Immutable Version + Mutable Deployment"
                    "（A/B / Canary / Rollback / Audit / Runtime Resolver）",
        lifespan=lifespan,
    )
    if runtime is not None:
        app.state.runtime = runtime

    app.include_router(prompts.router)
    app.include_router(configs.router)
    app.include_router(deployments.router)
    app.include_router(resolve.router)

    @app.exception_handler(ConfigRegistryError)
    async def domain_error_handler(request: Request, exc: ConfigRegistryError):
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
            "cache": rt.config.cache_backend,
        }

    return app


app = create_app()
