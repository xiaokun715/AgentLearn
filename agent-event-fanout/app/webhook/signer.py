"""Webhook 签名器 —— 发送侧封装（设计说明书 §12~§15）。

负责构造标准请求头：

    X-Event-ID: evt_xxx
    X-Event-Type: agent.job.completed
    X-Webhook-ID: delivery_xxx
    X-Webhook-Timestamp: 1756774800
    X-Webhook-Signature: v1=<hexdigest>
"""
from __future__ import annotations

import time

from ..security.signature import SignatureError, generate_signature, verify_signature
from .signature_header import (
    HEADER_EVENT_ID,
    HEADER_EVENT_TYPE,
    HEADER_WEBHOOK_ID,
    HEADER_WEBHOOK_SIGNATURE,
    HEADER_WEBHOOK_TIMESTAMP,
)


class Signer:
    """为一次投递生成签名与请求头。``verify_request`` 供客户侧/测试复用。"""

    def __init__(self, *, tolerance: int = 300) -> None:
        self.tolerance = tolerance

    def build_headers(
        self,
        *,
        secret: str,
        event_id: str,
        event_type: str,
        delivery_id: str,
        body: bytes,
        timestamp: int | None = None,
    ) -> dict[str, str]:
        ts = timestamp if timestamp is not None else int(time.time())
        signature = generate_signature(secret, ts, body)
        return {
            HEADER_EVENT_ID: event_id,
            HEADER_EVENT_TYPE: event_type,
            HEADER_WEBHOOK_ID: delivery_id,
            HEADER_WEBHOOK_TIMESTAMP: str(ts),
            HEADER_WEBHOOK_SIGNATURE: signature,
        }

    def verify_request(
        self,
        *,
        secret: str,
        timestamp: str,
        signature: str,
        body: bytes,
        now: float | None = None,
    ) -> None:
        try:
            ts = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise SignatureError("X-Webhook-Timestamp 不是有效整数") from exc
        verify_signature(secret, ts, body, signature, tolerance=self.tolerance, now=now)


__all__ = [
    "Signer",
    "SignatureError",
    "HEADER_EVENT_ID",
    "HEADER_EVENT_TYPE",
    "HEADER_WEBHOOK_ID",
    "HEADER_WEBHOOK_TIMESTAMP",
    "HEADER_WEBHOOK_SIGNATURE",
    "generate_signature",
    "verify_signature",
]
