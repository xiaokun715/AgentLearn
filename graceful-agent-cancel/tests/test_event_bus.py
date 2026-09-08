"""EventBus 单元测试：历史 ring buffer、实时订阅广播、终态识别。"""
from __future__ import annotations

from app.domain.events import EventBus, TERMINAL_EVENTS, TaskEventType


async def test_emit_and_history_are_ordered():
    bus = EventBus()
    bus.emit("t1", TaskEventType.TASK_STARTED, {"a": 1})
    bus.emit("t1", TaskEventType.STEP_STARTED, {"step": "collect"})
    hist = bus.history("t1")
    assert [e.seq for e in hist] == [1, 2]
    assert hist[0].event_type == "TASK_STARTED"
    assert hist[1].payload == {"step": "collect"}


async def test_history_is_per_task():
    bus = EventBus()
    bus.emit("t1", TaskEventType.TASK_STARTED)
    bus.emit("t2", TaskEventType.TASK_CREATED)
    assert [e.event_type for e in bus.history("t1")] == ["TASK_STARTED"]
    assert [e.event_type for e in bus.history("t2")] == ["TASK_CREATED"]


async def test_subscriber_receives_live_event():
    bus = EventBus()
    q = bus.subscribe("t1")
    bus.emit("t1", TaskEventType.TOOL_CHUNK, {"i": 1})
    ev = await q.get()
    assert ev.event_type == "TOOL_CHUNK"
    assert ev.payload == {"i": 1}
    bus.unsubscribe("t1", q)


async def test_terminal_event_set():
    names = {e.value for e in TERMINAL_EVENTS}
    assert "TASK_COMPLETED" in names
    assert "TASK_CANCELLED" in names
    assert "TASK_FAILED" in names
    assert "STEP_STARTED" not in names
