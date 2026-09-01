"""StreamEvent 模型单测（设计说明书 §5 / §7）。"""
from streaminfra.core.event import EventType, StreamError, StreamEvent


def test_event_defaults():
    ev = StreamEvent(stream_id="s", seq=1, type="token", data={"delta": "你"})
    assert ev.stream_id == "s"
    assert ev.seq == 1
    assert ev.type == "token"
    assert ev.data == {"delta": "你"}
    assert ev.timestamp > 0


def test_event_to_dict_roundtrip():
    ev = StreamEvent(stream_id="s", seq=10, type=EventType.TOKEN, data={"delta": "hello"})
    d = ev.to_dict()
    assert d["id"] == "s"
    assert d["seq"] == 10
    assert d["type"] == "token"
    assert d["data"] == {"delta": "hello"}
    assert d["timestamp"] == ev.timestamp
    ev2 = StreamEvent.from_dict(d)
    assert ev2 == ev


def test_event_types_values():
    assert EventType.TOKEN.value == "token"
    assert EventType.DONE.value == "done"
    assert EventType.ERROR.value == "error"
    assert EventType.HEARTBEAT.value == "heartbeat"


def test_stream_error_to_dict():
    err = StreamError(code="UPSTREAM_TIMEOUT", retryable=True, detail="boom")
    assert err.to_dict() == {"code": "UPSTREAM_TIMEOUT", "retryable": True, "detail": "boom"}
