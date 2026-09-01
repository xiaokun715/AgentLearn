"""SSE 编解码与响应头（设计说明书 §14 / §15 / §16）。

SSE 帧格式（§14）：
    id: {seq}
    event: {type}
    data: {json}

心跳用 SSE comment（§16）：中间层（Nginx/LB/Firewall）不会把它当业务事件。
"""
from __future__ import annotations

import json

from ..core.event import StreamEvent

# 必须设置 Content-Type: text/event-stream（§15）；
# 同时建议 no-cache / keep-alive；X-Accel-Buffering 让 Nginx 不缓冲。
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def encode_sse(event: StreamEvent) -> str:
    if event.type == "heartbeat":
        return ": heartbeat\n\n"
    return (
        f"id: {event.seq}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(event.data, ensure_ascii=False)}\n\n"
    )


def encode_done_sentinel() -> str:
    """OpenAI 风格结束符（设计说明书 §3.1 示例里的 data: [DONE]）。"""
    return "data: [DONE]\n\n"
