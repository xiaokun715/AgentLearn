"""Execution API 路由（设计说明书 §7 / §22）。

    POST /v1/executions              创建执行（queued）
    GET  /v1/executions/{id}         查询状态与结果
    POST /v1/executions/{id}/kill    Kill Switch（幂等）
    GET  /v1/executions              列出最近执行
    GET  /v1/policies                列出服务端策略
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from ..domain.exceptions import (
    ExecutionNotFound,
    InvalidToolType,
    PolicyNotFound,
    SandboxError,
)
from ..policy.compiler import SUPPORTED_TOOL_TYPES
from ..security.identity import identity_from_headers
from ..service import ExecutionService
from .schemas import ExecutionCreated, ExecutionRequest, ExecutionView

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["executions"])


def _service(request: Request) -> ExecutionService:
    return request.app.state.runtime.service


def _view(execution) -> ExecutionView:
    return ExecutionView(
        execution_id=execution.id,
        status=execution.status.value,
        policy_id=execution.policy_id,
        stdout=execution.stdout,
        stderr=execution.stderr,
        exit_code=execution.exit_code,
        error=execution.error,
        duration_ms=execution.duration_ms,
        resource_usage=execution.resource_usage,
        created_at=execution.created_at.isoformat() if execution.created_at else None,
    )


@router.post("/executions", response_model=ExecutionCreated, status_code=status.HTTP_201_CREATED)
async def create_execution(request: Request, body: ExecutionRequest) -> ExecutionCreated:
    service = _service(request)
    identity = identity_from_headers(request.headers)

    if body.type not in SUPPORTED_TOOL_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported tool type '{body.type}', supported: {SUPPORTED_TOOL_TYPES}",
        )

    try:
        execution = await service.create(
            tool_type=body.type,
            code=body.code,
            policy_request=body.policy.model_dump(exclude_none=True) if body.policy else None,
            identity=identity,
        )
    except PolicyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidToolType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ExecutionCreated(execution_id=execution.id, status=execution.status.value)


@router.get("/executions/{execution_id}", response_model=ExecutionView)
async def get_execution(request: Request, execution_id: str) -> ExecutionView:
    try:
        execution = await _service(request).get(execution_id)
    except ExecutionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _view(execution)


@router.post("/executions/{execution_id}/kill", response_model=ExecutionView)
async def kill_execution(request: Request, execution_id: str) -> ExecutionView:
    """Kill Switch（§22）：幂等 —— 重复 kill 返回同一 killed 结果，不报 500（§23）。"""
    try:
        execution = await _service(request).kill(execution_id)
    except ExecutionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _view(execution)


@router.get("/executions", response_model=list[ExecutionView])
async def list_executions(
    request: Request, limit: int = 50, tenant_id: str | None = None
) -> list[ExecutionView]:
    executions = await _service(request).list(limit=min(limit, 200), tenant_id=tenant_id)
    return [_view(e) for e in executions]


@router.get("/policies")
async def list_policies(request: Request) -> list[dict]:
    policies = await _service(request).list_policies()
    return [p.to_dict() for p in policies]
