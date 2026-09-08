"""FastAPI 观测服务入口。

启动：``uvicorn memory_engine.main:app --port 8000``
(或 ``python examples/live_server.py``)

lifecycle：构建/注入 Runtime → 启动 DecayWorker 后台代谢任务 → 挂到 app.state 供路由取用。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from .api import router as api_router
from .config import AgentMemoryConfig
from .factory import Runtime, build_runtime

logger = logging.getLogger("agentmemory")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 未显式注入 runtime 时按当前环境构建(含演示种子数据)
    runtime = app.state.runtime if hasattr(app.state, "runtime") else None
    if runtime is None:
        runtime = build_runtime(AgentMemoryConfig.from_env())
        app.state.runtime = runtime
    # 睡眠期遗忘代谢：后台定时任务(decay_interval_s 默认 60s，可 AGENTMEMORY_DECAY_INTERVAL_S 调)
    runtime.decay.start()
    logger.info("memory runtime ready | decay worker started")
    try:
        yield
    finally:
        await runtime.decay.stop()
        logger.info("memory runtime shutdown | decay worker stopped")


def create_app(
    config: Optional[AgentMemoryConfig] = None,
    runtime: Optional[Runtime] = None,
) -> FastAPI:
    app = FastAPI(
        title="Agent Memory Engine Observatory",
        description=(
            "第十一章 Agent 记忆系统 Demo —— 8 原语引擎 + 四层记忆 + 蒸馏/衰减 Worker 的观测 API。"
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    if runtime is not None:
        app.state.runtime = runtime

    app.include_router(api_router)

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


#: uvicorn / live_server 默认引用
app = create_app()
