"""DLQ API（设计说明书 §28）。

    GET  /v1/dlq              查看死信
    POST /v1/dlq/{job_id}/retry  人工修复后重新入队
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..factory import Runtime
from ..service import JobNotFoundError
from .schemas import DlqRetryResponse

router = APIRouter(prefix="/v1/dlq", tags=["dlq"])


@router.get("")
async def list_dlq(request: Request) -> list[dict]:
    rt: Runtime = request.app.state.runtime
    return await rt.dlq_service.list()


@router.post("/{job_id}/retry", response_model=DlqRetryResponse)
async def retry_dlq(job_id: str, request: Request) -> DlqRetryResponse:
    rt: Runtime = request.app.state.runtime
    try:
        job = await rt.job_service.get(job_id)
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    ok = await rt.dlq_service.retry(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail=f"job {job_id} is not in DLQ (status={job.status.value})")
    return DlqRetryResponse(job_id=job.id, status="queued")
