"""Task API —— 交互层：异步提交 / 查询 / 停止（对应第十章第 1 道防线）。

    POST /v1/tasks                     异步提交，立即返回 task_id（绝不阻塞等 Agent 跑完）
    GET  /v1/tasks/{task_id}           查询快照（状态 / 进度 / Partial Yield / 结果）
    POST /v1/tasks/{task_id}/cancel    CANCEL_TASK：红色 Stop 按钮的落点
    GET  /v1/tasks/{task_id}/history   事件历史（JSON，给轮询型客户端）
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..domain.exceptions import TaskNotFoundError, UnknownAgentError
from ..factory import Runtime
from .schemas import CancelRequest, SubmitRequest

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


def _rt(request: Request) -> Runtime:
    return request.app.state.runtime


@router.post("", status_code=202)
async def submit(body: SubmitRequest, request: Request) -> dict:
    rt = _rt(request)
    try:
        task = rt.service.submit(
            query=body.query, city=body.city, days=body.days, agent=body.agent,
        )
    except UnknownAgentError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # 立即返回 task_id —— HTTP 生命周期就此结束，Agent 生命周期才刚刚开始
    return {
        "task_id": task.task_id,
        "status": task.status.value,
        "events_url": f"/v1/tasks/{task.task_id}/events",
        "cancel_url": f"/v1/tasks/{task.task_id}/cancel",
        "note": "异步任务已受理；可通过 SSE(events_url) 实时监听进度，随时可按 Stop。",
    }


@router.get("/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    rt = _rt(request)
    try:
        return rt.service.get(task_id).to_public()
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, body: CancelRequest, request: Request) -> dict:
    rt = _rt(request)
    try:
        return rt.service.cancel(task_id, mode=body.mode)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/{task_id}/history")
async def task_history(task_id: str, request: Request) -> list[dict]:
    rt = _rt(request)
    try:
        return rt.service.history(task_id)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
