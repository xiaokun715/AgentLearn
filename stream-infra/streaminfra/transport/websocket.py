"""WebSocket 帧编解码（设计说明书 §17）。

服务器消息（扁平结构，§3.2 示例）：
    {"type": "token", "seq": 1, "delta": "你"}

客户端消息：
    {"type": "start", "stream_id": "...", "prompt": "...", "last_seq": 0}
    {"type": "cancel"}
    {"type": "ping"}
"""
from __future__ import annotations

from typing import Any, Dict

from ..core.event import StreamEvent


def encode_ws(event: StreamEvent) -> Dict[str, Any]:
    """把 StreamEvent 展平为 WebSocket JSON 消息。"""
    msg: Dict[str, Any] = {"type": event.type, "seq": event.seq}
    msg.update(event.data)
    return msg
