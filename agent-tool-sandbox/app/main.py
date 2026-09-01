"""应用入口（设计说明书 §41 完整架构：Agent → Policy → Sandbox → Runtime → Audit）。

启动（默认：SQLite + ProcessSandbox 兜底，零依赖）：
    uvicorn app.main:app --port 8000

Docker 版：
    SANDBOX_BACKEND=docker STORAGE_BACKEND=postgres \
    DATABASE_URL=postgresql://sandbox:sandbox@localhost:5432/sandbox \
    uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.execution import router as execution_router
from .config import AppConfig
from .factory import Runtime, build_runtime

logger = logging.getLogger(__name__)


def create_app(config: AppConfig | None = None, runtime: Runtime | None = None) -> FastAPI:
    """组装 FastAPI 应用。测试可通过注入 ``runtime`` 复用组件。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime if runtime is not None else await build_runtime(config)
        app.state.runtime = rt
        await rt.start()
        logger.info(
            "sandbox app started: storage=%s sandbox=%s",
            rt.config.storage_backend, rt.manager.get_sandbox().name,
        )
        yield
        await rt.stop()

    app = FastAPI(
        title="Agent Tool Sandbox",
        version="0.1.0",
        description="让 Agent 安全执行 Python / Shell / SQL 的沙箱 Runtime "
                    "(Policy → Compiler → Sandbox → Resource → Audit)",
        lifespan=lifespan,
    )
    app.include_router(execution_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
