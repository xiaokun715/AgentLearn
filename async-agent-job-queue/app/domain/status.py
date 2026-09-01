"""Job 状态枚举（设计说明书 §7）。"""
from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    """集中定义全部状态，状态迁移只允许通过 JobStateMachine 完成。"""

    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD = "dead"  # 进入 Dead Letter Queue

    @classmethod
    def from_str(cls, value: str | None) -> "JobStatus":
        if value is None:
            raise ValueError("status cannot be None")
        return cls(value)
