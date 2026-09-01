"""事件领域模型（设计说明书 §34-36）。

State = 快照（当前是什么状态）
Event = 历史（为什么变成这个状态）

两者必须同时保存：Job 表回答「现在」，job_events 表回答「怎么走到这里」，
并提供可观测性 / 审计能力。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobEventType(str, Enum):
    JOB_CREATED = "JOB_CREATED"
    JOB_STARTED = "JOB_STARTED"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    LLM_CALLED = "LLM_CALLED"
    LLM_COMPLETED = "LLM_COMPLETED"
    LLM_FAILED = "LLM_FAILED"
    TOOL_CALLED = "TOOL_CALLED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    TOOL_SKIPPED = "TOOL_SKIPPED"  # 幂等命中，跳过重放
    CHECKPOINT_SAVED = "CHECKPOINT_SAVED"
    JOB_RETRYING = "JOB_RETRYING"
    JOB_REQUEUED = "JOB_REQUEUED"
    JOB_RECOVERED = "JOB_RECOVERED"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_FAILED = "JOB_FAILED"
    JOB_CANCELLED = "JOB_CANCELLED"
    JOB_DEAD = "JOB_DEAD"


@dataclass
class JobEvent:
    """一条 append-only 的 Job 生命周期事件。"""

    job_id: str
    event_type: str
    payload: dict | None = None
    seq: int | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "job_id": self.job_id,
            "type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }
