"""客户侧 Webhook 校验工具（设计说明书 §15, §23）。

客户收到 Webhook 后，用它验证签名 + 时间窗，并按 ``X-Webhook-ID`` 幂等去重。
"""
from __future__ import annotations

from app.security.signature import SignatureError, verify_signature


def verify_webhook_request(*, secret: str, headers: dict[str, str], body: bytes,
                           tolerance: int = 300) -> str:
    """验证一次 Webhook 请求。

    返回 ``delivery_id``（幂等键）。验证失败抛 :class:`SignatureError`。

    - §14：Replay Protection —— 时间戳超出 5 分钟窗口拒绝。
    - §15：Constant-Time Comparison。
    - §23~§24：用 X-Webhook-ID 作为幂等键去重。
    """
    # 兼容各类 header 容器：httpx.Headers / Starlette Headers（大小写不敏感）
    # 与普通 dict（大小写敏感）。ASGI 规范里头名是小写，这里统一小写查找。
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    signature = h.get("x-webhook-signature", "")
    timestamp = h.get("x-webhook-timestamp", "")
    delivery_id = h.get("x-webhook-id", "")
    event_id = h.get("x-event-id", "")

    verify_signature(
        secret=secret,
        timestamp=int(timestamp),
        body=body,
        signature=signature,
        tolerance=tolerance,
    )
    return delivery_id


__all__ = ["verify_webhook_request", "SignatureError"]
