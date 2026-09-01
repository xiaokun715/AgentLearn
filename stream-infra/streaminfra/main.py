"""StreamInfra 应用装配（设计说明书 §39 / §46）。

启动：uvicorn streaminfra.main:app --reload
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .api import sse as sse_api
from .api import websocket as ws_api
from .buffer.base import ReplayBuffer
from .buffer.memory import MemoryReplayBuffer
from .buffer.redis import RedisReplayBuffer
from .config import BufferBackend, StreamConfig
from .core.manager import StreamManager
from .metrics.latency import MetricsRegistry
from .provider.mock_llm import MockLLMProvider


def _default_provider_factory(config: StreamConfig):
    def factory(prompt: str, stream_id: str) -> MockLLMProvider:
        return MockLLMProvider(
            tokens=config.mock_tokens,
            delay=config.provider_delay,
            input_tokens=config.mock_input_tokens,
            fail_after=config.mock_fail_after,
            tool_call_after=config.mock_tool_call_after,
        )

    return factory


def _default_buffer_factory(config: StreamConfig):
    async def factory(stream_id: str) -> ReplayBuffer:
        if config.buffer_backend == BufferBackend.REDIS:
            return RedisReplayBuffer(stream_id, config.redis_url, max_events=config.max_events)
        return MemoryReplayBuffer(max_events=config.max_events)

    return factory


def create_app(
    config: Optional[StreamConfig] = None,
    provider_factory: Optional[Callable[[str, str], Any]] = None,
    buffer_factory: Optional[Callable[[str], Any]] = None,
) -> FastAPI:
    config = config or StreamConfig.from_env()
    metrics = MetricsRegistry()
    provider_factory = provider_factory or _default_provider_factory(config)
    buffer_factory = buffer_factory or _default_buffer_factory(config)
    manager = StreamManager(provider_factory, buffer_factory, config, metrics)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await manager.close_all()

    app = FastAPI(title="StreamInfra", version="0.1.0", lifespan=lifespan)
    app.state.stream_manager = manager
    app.state.stream_config = config
    app.state.metrics = metrics

    app.include_router(sse_api.router)
    app.include_router(ws_api.router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "streams": manager.count()}

    @app.get("/metrics")
    async def metrics_endpoint():
        return PlainTextResponse(metrics.render_prometheus())

    @app.get("/v1/streams/{stream_id}/result")
    async def stream_result(stream_id: str):
        """流的最终/部分结果：status + content + usage + error（设计说明书 §27）。"""
        result = manager.result(stream_id)
        if result is None:
            return JSONResponse(
                status_code=404,
                content={"error": "stream_not_found", "stream_id": stream_id},
            )
        return result.to_dict()

    return app


app = create_app()
