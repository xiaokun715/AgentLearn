"""Event 入口 API（设计说明书 §32）。

POST /v1/events  创建事件（同步写库+Outbox，异步扇出，立即返回 accepted）
GET  /v1/events          列出事件
GET  /v1/events/{id}     查看事件
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..domain.exceptions import NotFoundError
from .schemas import EventAccepted, EventCreate, EventResponse

router = APIRouter(prefix="/v1/events", tags=["events"])


def _rt(request: Request):
    return request.app.state.runtime


def _to_response(event) -> EventResponse:
    return EventResponse(
        id=event.id,
        type=event.type,
        tenant_id=event.tenant_id,
        created_at=event.created_at.isoformat(),
        data=event.data,
        metadata=event.metadata,
    )


@router.post("", response_model=EventAccepted, status_code=202)
async def create_event(body: EventCreate, request: Request):
    """创建事件。

    不等待任何 Webhook（§32）：Event + Outbox 同事务提交后立即返回，
    后续由 Outbox Worker -> Queue -> Fan-out -> Webhook Worker 异步处理。
    """
    rt = _rt(request)
    event = await rt.event_service.create_event(
        body.type, body.data, metadata=body.metadata
    )
    return EventAccepted(event_id=event.id)


@router.get("", response_model=list[EventResponse])
async def list_events(request: Request, limit: int = 50):
    rt = _rt(request)
    events = await rt.event_service.list_events(limit=limit)
    return [_to_response(e) for e in events]


@router.get("/{event_id}", response_model=EventResponse)
async def get_event(event_id: str, request: Request):
    rt = _rt(request)
    event = await rt.event_service.get_event(event_id)
    if event is None:
        raise NotFoundError(f"Event {event_id} 不存在")
    return _to_response(event)
