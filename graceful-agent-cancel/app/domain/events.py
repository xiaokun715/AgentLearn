"""任务事件（Event）与轻量 EventBus。

EventBus 是四道防线之间“可观测”的中枢：
  - 存历史（ring buffer）供 SSE 断线重放 / GET history 查询；
  - 广播给实时订阅者（每个 SSE 长连接一个 asyncio.Queue）。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskEventType(str, Enum):
    TASK_CREATED = "TASK_CREATED"
    TASK_STARTED = "TASK_STARTED"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    LLM_THINK_STARTED = "LLM_THINK_STARTED"
    LLM_THINK_COMPLETED = "LLM_THINK_COMPLETED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_CHUNK = "TOOL_CHUNK"                 # 慢速下载过程中的分片（层3 可被掐断处）
    TOOL_COMPLETED = "TOOL_COMPLETED"
    FACT_COLLECTED = "FACT_COLLECTED"
    FINALIZE_STAGING = "FINALIZE_STAGING"     # 开始往临时表/临时文件写脏数据
    # ---- 取消链路 --------------------------------------------------------------
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"   # 网关收到用户 Stop（层1）
    LOOP_CANCEL_BREAK = "LOOP_CANCEL_BREAK"             # 循环层在动作边界捕获（层2）
    FORCE_CANCELLED = "FORCE_CANCELLED"                 # asyncio.CancelledError 掐断（层3）
    # ---- 兜底善后（层4） --------------------------------------------------------
    CLEANUP_STARTED = "CLEANUP_STARTED"
    DB_ROLLED_BACK = "DB_ROLLED_BACK"
    TEMP_CLEANED = "TEMP_CLEANED"
    PARTIAL_YIELD = "PARTIAL_YIELD"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_FAILED = "TASK_FAILED"


TERMINAL_EVENTS = {
    TaskEventType.TASK_COMPLETED,
    TaskEventType.TASK_CANCELLED,
    TaskEventType.TASK_FAILED,
}


@dataclass
class TaskEvent:
    """一条 append-only 的任务生命周期事件。"""

    task_id: str
    event_type: str
    payload: dict | None = None
    seq: int | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "task_id": self.task_id,
            "type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


class EventBus:
    """进程内事件中枢：历史 ring buffer + 实时订阅者广播。"""

    def __init__(self, window: int = 500) -> None:
        self._window = window
        self._history: dict[str, deque] = {}
        self._seq: dict[str, int] = {}
        self._subs: dict[str, set] = {}

    # ---- 发布 ----------------------------------------------------------------
    def emit(self, task_id: str, event_type: TaskEventType | str, payload: dict | None = None) -> TaskEvent:
        seq = self._seq.get(task_id, 0) + 1
        self._seq[task_id] = seq
        ev = TaskEvent(task_id=task_id, event_type=event_type.value if isinstance(event_type, Enum) else event_type,
                       payload=payload, seq=seq)
        q = self._history.setdefault(task_id, deque(maxlen=self._window))
        q.append(ev)
        for sub in list(self._subs.get(task_id, ())):
            sub.put_nowait(ev)  # 单事件循环，put_nowait 永不阻塞
        return ev

    def emit_now(self, task_id: str, event_type: TaskEventType | str, payload: dict | None = None) -> TaskEvent:
        return self.emit(task_id, event_type, payload)

    # ---- 订阅 / 重放 ---------------------------------------------------------
    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(task_id)
        if subs is not None:
            subs.discard(q)

    def history(self, task_id: str) -> list[TaskEvent]:
        return list(self._history.get(task_id, ()))
