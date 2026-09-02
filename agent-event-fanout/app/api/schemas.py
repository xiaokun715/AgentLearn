"""API 请求/响应模型（设计说明书 §31~§32, §39）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


# ---- Subscriber -----------------------------------------------------------
class SubscriberCreate(BaseModel):
    url: str = Field(..., description="客户 Webhook 地址")
    events: list[str] = Field(..., min_length=1, description="订阅的 Event Type 列表")

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        """§06：至少校验 scheme 与 host 存在（SSRF 的轻量防护）。"""
        from urllib.parse import urlparse

        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Webhook URL scheme 必须是 http/https，得到 '{parsed.scheme}'")
        if not parsed.hostname:
            raise ValueError("Webhook URL 缺少 host")
        return v

    @field_validator("events")
    @classmethod
    def _validate_events(cls, v: list[str]) -> list[str]:
        for ev in v:
            if len(ev.split(".")) < 2:
                raise ValueError(f"Event Type '{ev}' 不合法，应为 domain.resource.action")
        return v


class SubscriberResponse(BaseModel):
    id: str
    tenant_id: str
    url: str
    events: list[str]
    status: str
    created_at: str


class SubscriberCreated(BaseModel):
    id: str
    secret: str


class SubscriberUpdate(BaseModel):
    status: str | None = None
    events: list[str] | None = None


# ---- Event ----------------------------------------------------------------
class EventCreate(BaseModel):
    type: str = Field(..., description="Event Type，如 agent.job.completed")
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventResponse(BaseModel):
    id: str
    type: str
    tenant_id: str
    created_at: str
    data: dict[str, Any]
    metadata: dict[str, Any]


class EventAccepted(BaseModel):
    event_id: str
    status: str = "accepted"


# ---- Delivery -------------------------------------------------------------
class DeliveryResponse(BaseModel):
    id: str
    event_id: str
    subscriber_id: str
    status: str
    attempt_count: int
    next_retry_at: str | None
    last_error: str | None
    response_status: int | None
    created_at: str
    updated_at: str


class ReplayResponse(BaseModel):
    delivery_id: str
    status: str
