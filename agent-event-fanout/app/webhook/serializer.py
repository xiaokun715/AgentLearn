"""Event -> Webhook Body（设计说明书 §12）。"""
from __future__ import annotations

import json
from typing import Any

from ..domain.event import Event


def serialize_event(event: Event, *, include_metadata: bool = True) -> bytes:
    """把 Event 序列化为 Webhook 请求体（§12 的 Body）。

    Body 字段：``id / type / created_at / data``，可选 ``metadata``。
    """
    payload: dict[str, Any] = {
        "id": event.id,
        "type": event.type,
        "created_at": event.created_at.isoformat(),
        "data": event.data,
    }
    if include_metadata and event.metadata:
        payload["metadata"] = event.metadata
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")
