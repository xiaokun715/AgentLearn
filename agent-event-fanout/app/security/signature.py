"""Webhook 签名（设计说明书 §13~§15）。

- 使用 HMAC-SHA256，而不是 ``hash(secret + body)``（避免长度扩展攻击）。
- 签名内容包含 ``timestamp + "." + body``，防止 **Replay Attack**（§14）：
  攻击者截获请求后过几小时重放，会因时间窗过期而被拒绝。
- 验证必须用 ``hmac.compare_digest``（Constant-Time Comparison，§15），
  避免 timing attack。

约定：``X-Webhook-Signature: v1=<hexdigest>``
"""
from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_PREFIX = "v1="
DEFAULT_TOLERANCE = 300  # 秒（§14：只允许 5 分钟）


class SignatureError(Exception):
    """签名不合法 / 时间窗过期 / 算法不符。"""


def generate_signature(secret: str, timestamp: int, body: bytes) -> str:
    """生成签名：``HMAC(secret, f"{timestamp}.{body}")``。"""
    payload = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(
    secret: str,
    timestamp: int,
    body: bytes,
    signature: str,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> None:
    """验证签名 + 时间窗（§14 Replay Protection）。

    Raises:
        SignatureError: 时间窗过期 / 算法不符 / HMAC 不匹配。
    """
    if not signature.startswith(SIGNATURE_PREFIX):
        raise SignatureError("签名格式不合法，应为 'v1=<hexdigest>'")

    # 1) Replay Protection：时间窗检查
    current = now if now is not None else time.time()
    if abs(current - timestamp) > tolerance:
        raise SignatureError(
            f"时间戳 {timestamp} 超出允许窗口 {tolerance}s（Replay Protection）"
        )

    # 2) Constant-Time 比较（§15）
    expected = generate_signature(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("HMAC 签名不匹配（可能伪造了 body / 时间戳 / secret）")


def extract_signature(signature: str) -> str:
    """取 ``v1=xxx`` 中的 hexdigest 部分（预留多算法扩展）。"""
    return signature[len(SIGNATURE_PREFIX):]
