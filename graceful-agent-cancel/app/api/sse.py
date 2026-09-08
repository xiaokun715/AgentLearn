"""SSE 实时进度流（对应第十章第 1 道防线：WebSocket/SSE 长连接握手）。

前端提交任务拿到 ``task_id`` 后，用 ``EventSource`` 连接本端点，
就能看到 Agent 每一步、每个工具调用、以及取消链路的事件实时推下来。

实现要点：
  - 历史重放（断线/晚到客户端也能看到已发生的事件，用 seq 去重）；
  - 实时广播（每个 SSE 长连接在 EventBus 里订阅一个 asyncio.Queue）；
  - 推到终态事件（TASK_COMPLETED / TASK_CANCELLED / TASK_FAILED）后自动结束。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..domain.events import TERMINAL_EVENTS
from ..domain.exceptions import TaskNotFoundError
from ..factory import Runtime

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

_TERMINAL_NAMES = {e.value for e in TERMINAL_EVENTS}


def _fmt(event_type: str, ev_dict: dict) -> str:
    data = json.dumps(ev_dict, ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n"


async def _stream(rt: Runtime, task_id: str):
    """一次性 SSE 生成器：先重放历史，再消费实时队列，到终态即止。"""
    rt.service.get(task_id)  # 不存在会抛 TaskNotFoundError（已在端点里处理）

    q = rt.bus.subscribe(task_id)
    last_seq = 0
    try:
        for ev in rt.bus.history(task_id):
            last_seq = ev.seq or last_seq
            yield _fmt(ev.event_type, ev.to_dict())
            if ev.event_type in _TERMINAL_NAMES:
                return
        # 实时段
        while True:
            try:
                ev = await q.get()
            except asyncio.CancelledError:      # 客户端断开（EventSource.close）
                return
            if ev.seq is not None and ev.seq <= last_seq:
                continue
            last_seq = ev.seq or last_seq
            yield _fmt(ev.event_type, ev.to_dict())
            if ev.event_type in _TERMINAL_NAMES:
                return
    finally:
        rt.bus.unsubscribe(task_id, q)


@router.get("/{task_id}/events")
async def task_events(task_id: str, request: Request) -> StreamingResponse:
    rt: Runtime = request.app.state.runtime
    try:
        rt.service.get(task_id)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return StreamingResponse(
        _stream(rt, task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，保证事件即时到达
        },
    )
