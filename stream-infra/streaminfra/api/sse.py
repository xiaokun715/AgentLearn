"""SSE 接入（设计说明书 §3.1 / §14 / §19 / §22）。

GET /v1/chat/stream?stream_id=...&prompt=...
    通过请求头 Last-Event-ID 支持断线续传（浏览器原生支持）。
    流不存在            -> 404 stream_not_found
    last_seq 超出窗口   -> 409 resume_window_expired（§25）
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..buffer.base import ResumeWindowExpired
from ..core.event import StreamEvent
from ..core.manager import ConcurrentConsumer
from ..transport.sse import SSE_HEADERS, encode_done_sentinel, encode_sse

router = APIRouter()


@router.get("/v1/chat/stream")
async def chat_stream(
    request: Request,
    stream_id: Optional[str] = Query(default=None, description="复用/续传已有流；缺省则创建新流"),
    prompt: str = Query(default="你好，这是一个 Streaming Demo"),
    last_seq: int = Query(default=0, ge=0, description="客户端已收到的最大 seq"),
):
    manager = request.app.state.stream_manager
    config = request.app.state.stream_config

    # SSE 标准：浏览器断线重连会自动携带 Last-Event-ID 头（§22）
    header_id = request.headers.get("last-event-id")
    if header_id and header_id.isdigit():
        last_seq = int(header_id)

    new_stream = not stream_id
    if not stream_id:
        stream_id = await manager.create_stream(prompt=prompt)
    elif not manager.exists(stream_id):
        return JSONResponse(
            status_code=404,
            content={"error": "stream_not_found", "stream_id": stream_id},
        )

    # 在发送 Header 之前先校验 Replay Window，保证能返回 409（§25）
    stream = manager.get(stream_id)
    if last_seq > 0:
        try:
            await stream.buffer.validate_replay_window(stream_id, last_seq)
        except ResumeWindowExpired as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "resume_window_expired",
                    "stream_id": stream_id,
                    "last_seq": last_seq,
                    "oldest_seq": exc.oldest_seq,
                    "newest_seq": exc.newest_seq,
                    "reason": exc.reason,
                },
            )

    # 单消费者模型：已有活跃订阅者时拒绝第二个连接
    if stream.consumers > 0:
        return JSONResponse(
            status_code=409,
            content={"error": "concurrent_consumer", "stream_id": stream_id},
        )

    headers = dict(SSE_HEADERS)
    headers["X-Stream-Id"] = stream_id
    if new_stream:
        headers["X-Stream-Created"] = "true"

    async def gen():
        stream_obj = manager.get(stream_id)
        saw_done = False
        # 显式持有订阅器：async-for 的 break/GeneratorExit 不会自动 aclose 它，
        # 必须在 finally 中释放消费者槽位，否则重连会被误判为 concurrent_consumer。
        sub = manager.subscribe(
            stream_id, last_seq=last_seq, probe=lambda: request.is_disconnected()
        )
        try:
            async for event in sub:
                yield encode_sse(event)
                if event.type == "done":
                    saw_done = True
                    break
        except ConcurrentConsumer:
            # 已有活跃订阅者（竞态下走到这里）：Header 已发出，错误作为事件
            yield encode_sse(StreamEvent(stream_id, -1, "error", {"code": "concurrent_consumer", "stream_id": stream_id}))
            return
        finally:
            try:
                await sub.aclose()  # 释放消费者槽位
            except Exception:
                pass
            # 无论正常结束还是客户端断开，都确保上游被取消（§19 / §20）
            if stream_obj is not None and not stream_obj.is_terminal:
                await manager.disconnect(stream_id)
        if config.append_done_sentinel and saw_done:
            yield encode_done_sentinel()

    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)
