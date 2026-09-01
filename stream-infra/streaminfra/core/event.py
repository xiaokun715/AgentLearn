"""StreamEvent 统一事件模型（设计说明书 §5 / §7）。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """事件类型（设计说明书 §5）。"""
    START = "start"
    METADATA = "metadata"
    TOKEN = "token"
    TOOL_CALL = "tool_call"
    ERROR = "error"
    DONE = "done"
    HEARTBEAT = "heartbeat"


@dataclass(slots=True)
class StreamEvent:
    """一条流式事件。

    字段：
      stream_id  所属流
      seq        单调递增序号（断线重连的游标，设计说明书 §6）
      type       事件类型
      data       负载（token 事件为 {"delta": "..."}）
      timestamp  产生时间
    """

    stream_id: str
    seq: int
    type: str | EventType
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.stream_id,
            "seq": self.seq,
            "type": self.type.value if isinstance(self.type, EventType) else str(self.type),
            "data": self.data,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StreamEvent":
        return cls(
            stream_id=payload["id"],
            seq=payload["seq"],
            type=payload["type"],
            data=payload.get("data", {}),
            timestamp=payload.get("timestamp", time.time()),
        )


@dataclass(frozen=True, slots=True)
class StreamError:
    """流式错误描述（设计说明书 §26）。"""
    code: str
    retryable: bool = False
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "retryable": self.retryable}
        if self.detail is not None:
            d["detail"] = self.detail
        return d
