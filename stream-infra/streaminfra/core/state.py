"""流状态机（设计说明书 §8）。

状态：
    CREATED -> RUNNING -> COMPLETED / FAILED -> CLOSED
              RUNNING -> CANCELLED（断线/取消）-> RUNNING（重连恢复）/ CLOSED
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class StreamStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLOSED = "closed"


TERMINAL_STATUSES = frozenset(
    {StreamStatus.COMPLETED, StreamStatus.FAILED, StreamStatus.CLOSED}
)

_ALLOWED_TRANSITIONS: dict[StreamStatus, set[StreamStatus]] = {
    StreamStatus.CREATED: {
        StreamStatus.RUNNING, StreamStatus.COMPLETED, StreamStatus.FAILED,
        StreamStatus.CANCELLED, StreamStatus.CLOSED,
    },
    StreamStatus.RUNNING: {
        StreamStatus.COMPLETED, StreamStatus.FAILED, StreamStatus.CANCELLED, StreamStatus.CLOSED,
    },
    # CANCELLED 可恢复（重连后重新 RUNNING），也可最终关闭
    StreamStatus.CANCELLED: {StreamStatus.RUNNING, StreamStatus.CLOSED},
    StreamStatus.COMPLETED: {StreamStatus.CLOSED},
    StreamStatus.FAILED: {StreamStatus.CLOSED},
    StreamStatus.CLOSED: set(),
}


class InvalidTransition(Exception):
    """非法的状态迁移。"""


@dataclass(slots=True)
class StreamState:
    stream_id: str
    status: StreamStatus = StreamStatus.CREATED
    created_at: float = field(default_factory=time.time)
    first_token_at: float | None = None
    completed_at: float | None = None
    error_code: str | None = None
    last_seq: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def transition(self, new: StreamStatus, *, error_code: str | None = None) -> None:
        if new not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransition(f"非法状态迁移: {self.status.value} -> {new.value}")
        self.status = new
        if error_code is not None:
            self.error_code = error_code
        if new is StreamStatus.RUNNING:
            # 从 CANCELLED 恢复：重新计时
            self.completed_at = None
        elif new is not StreamStatus.CREATED and self.completed_at is None:
            self.completed_at = time.time()

    # ---- 指标辅助（设计说明书 §28 / §30）----
    def ttft(self) -> float | None:
        """Time To First Token = first_token_at - created_at。"""
        if self.first_token_at is None:
            return None
        return self.first_token_at - self.created_at

    def total_latency(self) -> float | None:
        """Request -> DONE / terminal。"""
        if self.completed_at is None:
            return None
        return self.completed_at - self.created_at
