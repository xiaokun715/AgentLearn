"""Runtime Resolve API —— 最重要的接口（设计说明书 §21 / §31）。

  GET  /v1/resolve?agent=...&environment=...&user_id=...

返回 Agent Runtime 直接可用的配置快照（含 A/B routing 元数据）。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request


router = APIRouter(prefix="/v1", tags=["resolve"])


@router.get("/resolve")
async def resolve(
    request: Request,
    agent: str = Query(..., min_length=1),
    environment: str = Query(..., min_length=1),
    user_id: str = Query("", description="用于粘性 A/B 分桶"),
):
    rt = request.app.state.runtime
    snapshot = await rt.resolver.resolve(agent, environment, user_id)
    data = snapshot.to_dict()
    # 额外带上「执行身份」，方便 Agent Runtime 直接写进 Trace / Token Meter（§34~§35）
    data["execution_identity"] = snapshot.execution_identity()
    return data


@router.get("/audit")
async def list_audit(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    action: str | None = Query(None),
    agent: str | None = Query(None, description="按 agent 名过滤"),
):
    rt = request.app.state.runtime
    entries = await rt.audit_service.list(limit=limit, action=action, agent=agent)
    return [e.to_dict() for e in entries]
