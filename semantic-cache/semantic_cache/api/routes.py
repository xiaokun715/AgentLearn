"""HTTP API（设计说明书 §48）。

  POST  /v1/chat            聊天入口：Exact -> Semantic -> LLM -> 写缓存
  GET   /metrics            Prometheus 指标（§41）
  GET   /health             健康检查
  POST  /admin/invalidate   主动失效（§24）

实例通过 ``app.state`` 注入（见 main.py），与 stream-infra 的装配方式一致。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from ..core.entry import ChatRequest as ChatRequestDomain
from .schemas import ChatRequestIn, InvalidateRequest

router = APIRouter()


@router.post("/v1/chat")
async def chat(body: ChatRequestIn, request: Request):
    cache = request.app.state.cache
    llm = request.app.state.llm
    started = time.perf_counter()

    domain = ChatRequestDomain.from_dict(body.model_dump())

    # 先查缓存：Exact -> Semantic（§49）
    result = await cache.get(domain)

    if result.hit:
        response = dict(result.response)
        response["cache"] = {
            "hit": True,
            "source": result.source,
            "similarity": result.similarity,
            "confidence": result.confidence,
        }
        response["cache"]["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return response

    # MISS：调用 LLM，再把答案写入缓存（§34）
    response = await llm.chat(domain)
    await cache.set(domain, response)  # 不可缓存的内容会被 Cacheability Policy 拒绝（§35）
    response["cache"] = {
        "hit": False,
        "source": "miss",
        "similarity": 0.0,
        "confidence": 0.0,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    return response


@router.get("/metrics")
async def metrics_endpoint(request: Request):
    """Prometheus 文本格式指标（§41 ~ §46）。"""
    return PlainTextResponse(request.app.state.metrics.render_prometheus())


@router.get("/health")
async def health(request: Request):
    cache = request.app.state.cache
    return {
        "status": "ok",
        "cache_size": await cache.store.count(),
        "hit_rate": cache.metrics.hit_rate(),
    }


@router.post("/admin/invalidate")
async def invalidate(body: InvalidateRequest, request: Request):
    """主动失效（§24 / §25）。返回删除条数。"""
    cache = request.app.state.cache
    deleted = await cache.invalidate(
        cache_id=body.cache_id,
        namespace=body.namespace,
        tenant_id=body.tenant_id,
        model=body.model,
        knowledge_version=body.knowledge_version,
        agent_type=body.agent_type,
        task_type=body.task_type,
    )
    return {"deleted": deleted, "cache_size": await cache.store.count()}
