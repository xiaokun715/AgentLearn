"""Execution 领域对象（设计说明书 §8-9）。

状态机：
    QUEUED → POLICY_CHECK → STARTING → RUNNING → SUCCEEDED
                                        ├─ REJECTED（策略拒绝）
                                        ├─ FAILED
                                        ├─ TIMEOUT
                                        ├─ OOM
                                        ├─ KILLED（kill switch）
                                        └─ OUTPUT_LIMIT_EXCEEDED

所有状态迁移集中在 service/executor 中完成，这里只定义模型与终结态判断。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ExecutionStatus(str, Enum):
    """一次沙箱执行的生命周期状态。"""

    QUEUED = "queued"
    POLICY_CHECK = "policy_check"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    OOM = "oom"
    KILLED = "killed"
    REJECTED = "rejected"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"

    @classmethod
    def from_str(cls, value: str | None) -> "ExecutionStatus":
        if value is None:
            raise ValueError("status cannot be None")
        return cls(value)

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.TIMEOUT,
        ExecutionStatus.OOM,
        ExecutionStatus.KILLED,
        ExecutionStatus.REJECTED,
        ExecutionStatus.OUTPUT_LIMIT_EXCEEDED,
    }
)


@dataclass(slots=True)
class Execution:
    """一次沙箱执行（对应 §34 executions 表的一行）。"""

    id: str
    tenant_id: str
    tool_type: str          # python | shell | node | sql
    code: str               # Agent 产生的代码（UNTRUSTED INPUT）
    status: ExecutionStatus
    policy_id: str
    user_id: str = "anonymous"     # §33/§34 审计字段
    agent_id: str = "anonymous"
    runtime_id: str | None = None   # 进程 pid / 容器 id
    container_id: str | None = None  # Docker 专用
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    resource_usage: dict = field(default_factory=dict)
    kill_requested: bool = False     # kill switch 置位标记（§22）

    def mark_finished(self, status: ExecutionStatus) -> None:
        self.status = status
        self.finished_at = datetime.now(timezone.utc)
        if self.started_at is not None:
            self.duration_ms = int(
                (self.finished_at - self.started_at).total_seconds() * 1000
            )

    def to_audit_dict(self) -> dict:
        return {
            "execution_id": self.id,
            "tenant_id": self.tenant_id,
            "tool": self.tool_type,
            "policy": self.policy_id,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "resource_usage": self.resource_usage,
        }
