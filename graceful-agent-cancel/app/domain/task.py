"""AgentTask —— 一次正在飞奔的 Agent 任务快照。

注意与“取消令牌 / asyncio.Task”区分：
    AgentTask（本文件）    = 业务记录（状态 / 进度 / 已收集事实 / 部分产出）
    asyncio.Task（运行时） = 真正执行 agent 的协程句柄，取消它才可能掐断 I/O
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED)


@dataclass
class AgentTask:
    """内存中的任务记录（生产环境对应 Job/Redis 中的一行）。"""

    query: str
    city: str = "上海"
    days: int = 2
    agent_name: str = "research_agent"

    task_id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:12]}")
    status: TaskStatus = TaskStatus.RUNNING
    cancel_requested: bool = False

    current_step: str | None = None
    step_index: int = 0
    total_steps: int = 0
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    # 已收集的“事实”——Partial Yield 与最终报告都基于它
    facts: list[dict] = field(default_factory=list)

    # 兜底层产出
    partial_yield: str | None = None
    partial_meta: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None

    def collect(self, kind: str, text: str) -> None:
        self.facts.append({"kind": kind, "text": text})

    def to_public(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent": self.agent_name,
            "status": self.status.value,
            "cancel_requested": self.cancel_requested,
            "query": self.query,
            "city": self.city,
            "days": self.days,
            "current_step": self.current_step,
            "step_index": self.step_index,
            "total_steps": self.total_steps,
            "facts_count": len(self.facts),
            "facts": self.facts,
            "partial_yield": self.partial_yield,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }
