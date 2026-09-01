"""Delivery / DLQ 查询与管理 API（设计说明书 §21, §39）。

GET  /v1/deliveries?status=DLQ          查询投递（可按状态过滤）
GET  /v1/deliveries/{id}                查询单条投递（§39）
POST /v1/deliveries/{id}/replay         DLQ -> PENDING 重新投递（§21）
POST /v1/deliveries/{id}/cancel         取消投递
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..domain.exceptions import NotFoundError
from .schemas import DeliveryResponse, ReplayResponse

router = APIRouter(prefix="/v1/deliveries", tags=["deliveries"])


def _rt(request: Request):
    return request.app.state.runtime


def _to_response(delivery) -> DeliveryResponse:
    return DeliveryResponse(
        id=delivery.id,
        event_id=delivery.event_id,
        subscriber_id=delivery.subscriber_id,
        status=delivery.status,
        attempt_count=delivery.attempt_count,
        next_retry_at=delivery.next_retry_at.isoformat() if delivery.next_retry_at else None,
        last_error=delivery.last_error,
        response_status=delivery.response_status,
        created_at=delivery.created_at.isoformat(),
        updated_at=delivery.updated_at.isoformat(),
    )


@router.get("", response_model=list[DeliveryResponse])
async def list_deliveries(
    request: Request,
    status: str | None = Query(None, description="按状态过滤，如 SUCCESS/RETRYING/DLQ"),
    limit: int = 100,
):
    rt = _rt(request)
    deliveries = await rt.repo.list_deliveries(status=status, limit=limit)
    return [_to_response(d) for d in deliveries]


@router.get("/{delivery_id}", response_model=DeliveryResponse)
async def get_delivery(delivery_id: str, request: Request):
    rt = _rt(request)
    delivery = await rt.repo.get_delivery(delivery_id)
    if delivery is None:
        raise NotFoundError(f"Delivery {delivery_id} 不存在")
    return _to_response(delivery)


@router.post("/{delivery_id}/replay", response_model=ReplayResponse)
async def replay_delivery(delivery_id: str, request: Request):
    """§21：DLQ 重放 —— 状态置回 PENDING 并重新入队。"""
    rt = _rt(request)
    delivery = await rt.dlq_service.replay(delivery_id)
    return ReplayResponse(delivery_id=delivery.id, status=delivery.status)


@router.post("/{delivery_id}/cancel", response_model=ReplayResponse)
async def cancel_delivery(delivery_id: str, request: Request):
    """取消投递：DLQ -> CANCELLED。"""
    rt = _rt(request)
    delivery = await rt.dlq_service.cancel(delivery_id)
    return ReplayResponse(delivery_id=delivery.id, status=delivery.status)
