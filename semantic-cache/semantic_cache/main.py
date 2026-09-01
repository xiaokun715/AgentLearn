"""SemanticCache 应用装配（设计说明书 §48）。

启动：
  uvicorn semantic_cache.main:app --reload --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import routes
from .config import CacheConfig
from .factory import build_cache
from .llm.mock import MockLLM


def create_app(
    config: CacheConfig | None = None,
    *,
    cache=None,
    llm=None,
) -> FastAPI:
    config = config or CacheConfig.from_env()
    cache = cache or build_cache(config)
    llm = llm or MockLLM()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await cache.store.close()

    app = FastAPI(title="SemanticCache", version="0.1.0", lifespan=lifespan)
    app.state.cache = cache
    app.state.llm = llm
    app.state.metrics = cache.metrics

    app.include_router(routes.router)
    return app


app = create_app()
