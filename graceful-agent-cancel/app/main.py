"""应用入口。

启动：
    uvicorn app.main:app --port 8000
然后浏览器打开 http://localhost:8000/ 玩那个红色 Stop 按钮控制台。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import sse_router, tasks_router, ui_router
from .config import AgentConfig
from .factory import Runtime, build_runtime

logger = logging.getLogger(__name__)


def create_app(
    config: AgentConfig | None = None,
    runtime: Runtime | None = None,
) -> FastAPI:
    """组装 FastAPI 应用。测试可通过注入 ``runtime`` 复用组件、跳过 lifespan。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = getattr(app.state, "runtime", None)
        if rt is None:
            rt = runtime if runtime is not None else build_runtime(config)
            app.state.runtime = rt
        logger.info("app started: default_cancel_mode=%s", rt.config.default_cancel_mode)
        yield
        # 关闭时把仍在运行的 Agent 全部掐断（优雅停机）
        for task_id in rt.registry.running_task_ids():
            rt.service.cancel(task_id, mode="force")

    app = FastAPI(
        title="Agent 优雅中止 (Graceful Agent Cancel)",
        version="0.1.0",
        description="四道防线 Demo：交互层(SSE+Stop) / 循环层(取消令牌) / "
                    "底层(asyncio cancel) / 兜底层(回滚 + Partial Yield)",
        lifespan=lifespan,
    )
    if runtime is not None:
        app.state.runtime = runtime
    app.include_router(tasks_router)
    app.include_router(sse_router)
    app.include_router(ui_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
