"""Delivery 领域模型与状态机（设计说明书 §09~§11, §20）。

> Event 表示「发生了什么」，Delivery 表示「这个事件发送给谁、发送到什么状态」。

核心约束：``UNIQUE(event_id, subscriber_id)`` —— 同一个 Event 不会为同一个
Subscriber 创建多个 Delivery（§09，幂等的地基）。

状态机（§10）：:

    PENDING -> DELIVERING -> SUCCESS
                      \-> FAILED -> RETRYING -> DELIVERING ...
                                         \-> DLQ
    PENDING -> CANCELLED
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .event import new_id, utcnow
from .exceptions import InvalidStateError

# ---- Delivery 状态常量（§10） ------------------------------------------------
PENDING = "PENDING"
DELIVERING = "DELIVERING"
SUCCESS = "SUCCESS"
RETRYING = "RETRYING"
FAILED = "FAILED"
DLQ = "DLQ"
CANCELLED = "CANCELLED"

# 允许的状态迁移表：key = 当前状态，value = 可达状态集合
_TRANSITIONS: dict[str, set[str]] = {
    PENDING: {DELIVERING, CANCELLED},
    DELIVERING: {SUCCESS, FAILED, RETRYING, DLQ},
    RETRYING: {DELIVERING, CANCELLED},
    FAILED: {DLQ, RETRYING, CANCELLED},
    DLQ: {PENDING, CANCELLED},  # DLQ -> PENDING 即 Replay（§21）
    SUCCESS: set(),
    CANCELLED: set(),
}


def is_terminal(status: str) -> bool:
    return status in (SUCCESS, CANCELLED)


@dataclass(slots=True)
class Delivery:
    id: str
    event_id: str
    subscriber_id: str
    status: str = PENDING
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    last_error: str | None = None
    response_status: int | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @classmethod
    def create(cls, *, event_id: str, subscriber_id: str) -> "Delivery":
        return cls(
            id=new_id("del"),
            event_id=event_id,
            subscriber_id=subscriber_id,
        )

    def _transit(self, new_status: str) -> None:
        if new_status not in _TRANSITIONS.get(self.status, set()):
            raise InvalidStateError(
                f"Delivery {self.id} 非法状态迁移: {self.status} -> {new_status}"
            )
        self.status = new_status
        self.updated_at = utcnow()

    # ---- 业务动作 ----------------------------------------------------------
    def start_delivery(self) -> None:
        """Worker 开始投递：PENDING/RETRYING -> DELIVERING。"""
        self._transit(DELIVERING)
        self.attempt_count += 1

    def mark_success(self, response_status: int) -> None:
        """投递成功：DELIVERING -> SUCCESS。"""
        self._transit(SUCCESS)
        self.response_status = response_status
        self.last_error = None
        self.next_retry_at = None

    def mark_failed(self, error: str, response_status: int | None) -> None:
        """一次投递失败（进入重试判定）：DELIVERING -> FAILED。"""
        self._transit(FAILED)
        self.last_error = error
        self.response_status = response_status

    def schedule_retry(self, next_retry_at: datetime) -> None:
        """判定仍可重试：FAILED -> RETRYING。"""
        self._transit(RETRYING)
        self.next_retry_at = next_retry_at

    def dead_letter(self, reason: str) -> None:
        """超过最大重试：FAILED -> DLQ（§20）。"""
        self._transit(DLQ)
        self.last_error = reason
        self.next_retry_at = None

    def cancel(self) -> None:
        """人工取消：PENDING/RETRYING/DLQ -> CANCELLED。"""
        self._transit(CANCELLED)

    def replay(self) -> None:
        """DLQ -> PENDING（§21：管理端 Replay 后重新入队）。"""
        self._transit(PENDING)
        self.next_retry_at = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "subscriber_id": self.subscriber_id,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "last_error": self.last_error,
            "response_status": self.response_status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
