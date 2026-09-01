"""Job API（设计说明书 §8-10 / §36）。

    POST /v1/jobs                 创建任务，立即返回 job_id
    GET  /v1/jobs/{job_id}        查询状态
    POST /v1/jobs/{job_id}/cancel 取消
    GET  /v1/jobs/{job_id}/events 事件历史
    GET  /metrics                 指标（Prometheus 文本格式）
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..factory import Runtime
from ..service import AgentNotFoundError, JobNotFoundError
from .schemas import CancelJobResponse, CreateJobRequest, CreateJobResponse

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


@router.post("", response_model=CreateJobResponse, status_code=201)
async def create_job(body: CreateJobRequest, request: Request) -> CreateJobResponse:
    rt = _runtime(request)
    try:
        job = await rt.job_service.create(
            agent=body.agent,
            input=body.input,
            tenant_id=body.tenant_id,
            priority=body.priority,
            max_retries=body.max_retries or rt.config.max_retries,
        )
    except AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    # 立即返回，绝不等 Agent 执行（§8）
    return CreateJobResponse(job_id=job.id, status=job.status.value)


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> dict:
    rt = _runtime(request)
    try:
        job = await rt.job_service.get(job_id)
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return job.to_public()


@router.post("/{job_id}/cancel", response_model=CancelJobResponse)
async def cancel_job(job_id: str, request: Request) -> CancelJobResponse:
    rt = _runtime(request)
    try:
        job = await rt.job_service.cancel(job_id)
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    note = (
        "cancel signal sent; worker will stop at next checkpoint boundary"
        if job.cancel_requested
        else None
    )
    return CancelJobResponse(job_id=job.id, status=job.status.value, note=note)


@router.get("/{job_id}/events")
async def job_events(job_id: str, request: Request) -> list[dict]:
    rt = _runtime(request)
    try:
        events = await rt.job_service.events(job_id)
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return [e.to_dict() for e in events]
