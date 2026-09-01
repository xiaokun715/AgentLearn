"""WebSocket 接入（设计说明书 §17）。

ws://localhost:8000/v1/ws

客户端协议：
    启动  {"type": "start", "stream_id": "...", "prompt": "...", "last_seq": 0}
    取消  {"type": "cancel"}
    心跳  {"type": "ping"}            -> 服务端回 {"type": "pong"}

服务端消息：{"type": "token|metadata|tool_call|error|done|heartbeat", "seq": N, ...}
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..buffer.base import ResumeWindowExpired
from ..core.event import StreamEvent
from ..core.manager import ConcurrentConsumer
from ..transport.websocket import encode_ws

router = APIRouter()


@router.websocket("/v1/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    manager = websocket.app.state.stream_manager
    config = websocket.app.state.stream_config

    control_q: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
    disconnected = asyncio.Event()

    async def receive_loop() -> None:
        """后台接收客户端消息；连接断开时置 disconnected。"""
        try:
            while True:
                raw = await websocket.receive_json()
                await control_q.put(raw)
        except Exception:  # WebSocketDisconnect / 协议错误
            disconnected.set()

    recv_task = asyncio.create_task(receive_loop())

    stream_id: str | None = None
    last_seq = 0
    pump_task: asyncio.Task | None = None
    event_task: asyncio.Task | None = None
    ctrl_task: asyncio.Task | None = None
    end_task: asyncio.Task | None = None

    try:
        # ---------------- 1) 等待 start 消息 ----------------
        while stream_id is None:
            try:
                msg = await asyncio.wait_for(control_q.get(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "error", "code": "no_start_message"})
                return
            if msg.get("type") != "start":
                await websocket.send_json({"type": "error", "code": "expected_start"})
                continue

            try:
                last_seq = int(msg.get("last_seq") or 0)
            except (TypeError, ValueError):
                await websocket.send_json({
                    "type": "error", "code": "invalid_last_seq",
                    "detail": f"last_seq 必须是整数，收到 {msg.get('last_seq')!r}",
                })
                return
            if last_seq < 0:
                await websocket.send_json({"type": "error", "code": "invalid_last_seq", "detail": "last_seq 不能为负"})
                return

            sid = msg.get("stream_id")
            if sid:
                if not manager.exists(sid):
                    await websocket.send_json({"type": "error", "code": "stream_not_found", "stream_id": sid})
                    return
                if last_seq > 0:
                    stream = manager.get(sid)
                    try:
                        await stream.buffer.validate_replay_window(sid, last_seq)
                    except ResumeWindowExpired as exc:
                        await websocket.send_json({
                            "type": "error", "code": "resume_window_expired",
                            "stream_id": sid, "last_seq": last_seq,
                            "oldest_seq": exc.oldest_seq, "newest_seq": exc.newest_seq,
                            "reason": exc.reason,
                        })
                        return
                if stream.consumers > 0:
                    await websocket.send_json({"type": "error", "code": "concurrent_consumer", "stream_id": sid})
                    return
                stream_id = sid
                await websocket.send_json({"type": "resumed", "stream_id": sid, "last_seq": last_seq})
            else:
                stream_id = await manager.create_stream(
                    prompt=msg.get("prompt", "你好，这是一个 Streaming Demo")
                )
                await websocket.send_json({"type": "started", "stream_id": stream_id, "last_seq": 0})

        # ---------------- 2) 消费事件 ----------------
        # 泵任务把 subscribe 的事件灌入有界 event_q。
        # event_q 有界 => WS 客户端消费慢时，泵会被阻塞，进而让 Producer 背压。
        event_q: asyncio.Queue = asyncio.Queue(maxsize=config.queue_size)
        pump_done = asyncio.Event()

        async def pump() -> None:
            # 显式持有订阅器并在 finally 中 aclose，避免 async-for 中断时泄漏消费者槽位
            sub = manager.subscribe(
                stream_id, last_seq=last_seq, probe=lambda: disconnected.is_set()
            )
            try:
                async for event in sub:
                    await event_q.put(event)
            except ConcurrentConsumer:
                # 竞态下才可能走到这里（start 校验与 pump 启动之间有人抢占）
                await event_q.put(
                    StreamEvent(stream_id, -1, "error", {"code": "concurrent_consumer", "stream_id": stream_id})
                )
            finally:
                try:
                    await sub.aclose()
                except Exception:
                    pass
                pump_done.set()

        pump_task = asyncio.create_task(pump())

        while True:
            if event_task is None:
                event_task = asyncio.ensure_future(event_q.get())
            if ctrl_task is None:
                ctrl_task = asyncio.ensure_future(control_q.get())
            if end_task is None:
                end_task = asyncio.ensure_future(pump_done.wait())

            done, _ = await asyncio.wait(
                {event_task, ctrl_task, end_task}, return_when=asyncio.FIRST_COMPLETED
            )

            if event_task in done:
                value = event_task.result()
                event_task = None
                await websocket.send_json(encode_ws(value))

            if ctrl_task in done:
                value = ctrl_task.result()
                ctrl_task = None
                mtype = value.get("type")
                if mtype == "cancel":
                    await manager.cancel(stream_id, reason="client_cancel")
                    await websocket.send_json({"type": "done", "reason": "cancelled"})
                    break
                elif mtype == "ping":
                    await websocket.send_json({"type": "pong"})
                # 忽略重复 start

            if end_task in done:
                # 泵结束（subscribe 返回）：排空剩余事件后退出
                while True:
                    try:
                        value = event_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await websocket.send_json(encode_ws(value))
                break

    except WebSocketDisconnect:
        pass  # 交给 finally 做上游取消
    finally:
        for t in (event_task, ctrl_task, end_task, pump_task, recv_task):
            if t is not None:
                t.cancel()
        for t in (event_task, ctrl_task, end_task, pump_task, recv_task):
            if t is not None:
                try:
                    await t
                except BaseException:
                    pass
        if stream_id and manager.exists(stream_id):
            stream = manager.get(stream_id)
            if stream is not None and not stream.is_terminal:
                await manager.disconnect(stream_id)
