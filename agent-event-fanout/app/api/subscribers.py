"""Subscriber 注册 API（设计说明书 §31）。

POST /v1/subscribers  创建订阅者（返回 secret，仅此一次）
GET  /v1/subscribers  列出订阅者
GET  /v1/subscribers/{id}
PATCH /v1/subscribers/{id}  更新 status / events
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request

from ..domain.exceptions import NotFoundError
from ..domain.subscriber import Subscriber
from .schemas import SubscriberCreate, SubscriberCreated, SubscriberResponse, SubscriberUpdate

router = APIRouter(prefix="/v1/subscribers", tags=["subscribers"])


def _rt(request: Request):
    return request.app.state.runtime


def _secret() -> str:
    return f"whsec_{secrets.token_hex(16)}"


def _to_response(sub: Subscriber) -> SubscriberResponse:
    return SubscriberResponse(
        id=sub.id,
        tenant_id=sub.tenant_id,
        url=sub.url,
        events=sub.events,
        status=sub.status,
        created_at=sub.created_at.isoformat(),
    )


@router.post("", response_model=SubscriberCreated, status_code=201)
async def create_subscriber(body: SubscriberCreate, request: Request):
    """创建 Subscriber（§06）。secret 仅返回一次，客户用它验证签名（§13）。"""
    rt = _rt(request)
    subscriber = Subscriber.create(
        url=body.url,
        events=body.events,
        tenant_id=rt.config.tenant_id,
        secret=_secret(),
    )
    await rt.repo.create_subscriber(subscriber)
    return SubscriberCreated(id=subscriber.id, secret=subscriber.secret)


@router.get("", response_model=list[SubscriberResponse])
async def list_subscribers(request: Request):
    rt = _rt(request)
    subs = await rt.repo.list_subscribers(tenant_id=rt.config.tenant_id)
    return [_to_response(s) for s in subs]


@router.get("/{subscriber_id}", response_model=SubscriberResponse)
async def get_subscriber(subscriber_id: str, request: Request):
    rt = _rt(request)
    sub = await rt.repo.get_subscriber(subscriber_id)
    if sub is None:
        raise NotFoundError(f"Subscriber {subscriber_id} 不存在")
    return _to_response(sub)


@router.patch("/{subscriber_id}", response_model=SubscriberResponse)
async def update_subscriber(subscriber_id: str, body: SubscriberUpdate, request: Request):
    """更新 status（active/disabled）或替换订阅的 events 列表（§07）。"""
    rt = _rt(request)
    sub = await rt.repo.get_subscriber(subscriber_id)
    if sub is None:
        raise NotFoundError(f"Subscriber {subscriber_id} 不存在")
    if body.status is not None:
        if body.status not in ("active", "disabled"):
            from ..domain.exceptions import EventFanoutError

            raise EventFanoutError(f"status 只能为 active/disabled，得到 {body.status}")
        await rt.repo.set_subscriber_status(subscriber_id, body.status)
        sub.status = body.status
    if body.events is not None:
        await rt.repo.replace_subscriber_events(subscriber_id, body.events)
        sub.events = body.events
    return _to_response(sub)
