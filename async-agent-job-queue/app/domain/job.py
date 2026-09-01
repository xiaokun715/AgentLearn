"""Job 领域模型（设计说明书 §5）。"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .status import JobStatus


def new_job_id() -> str:
    """生成 `job_xxxx` 样式的任务 ID。"""
    return f"job_{uuid.uuid4().hex[:12]}"


@dataclass
class Job:
    """一个持久化的 Agent 异步任务。

    - ``input``       由用户提交的 agent 输入（JSON 兼容 dict）。
    - ``status``      只能通过 JobStateMachine 校验后变更。
    - ``lease_*``     租约字段：Worker 崩溃后由 Reaper 依据过期时间回收（§29-31）。
    - ``cancel_requested`` 取消信号：Cancel 不是简单改 DB 状态（§10），
                        而是先置位该标志，Worker 在执行边界感知后协作停止。
    """

    id: str
    tenant_id: str
    agent_name: str
    input: dict
    status: JobStatus
    priority: int = 0
    retry_count: int = 0
    max_retries: int = 3
    current_step: str | None = None
    progress: int = 0
    worker_id: str | None = None
    lease_expire_at: float | None = None  # epoch seconds
    cancel_requested: bool = False
    queued_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---- 构造辅助 --------------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        agent_name: str,
        input: dict,
        tenant_id: str = "default",
        priority: int = 0,
        max_retries: int = 3,
        id: str | None = None,
    ) -> "Job":
        now = time.time()
        return cls(
            id=id or new_job_id(),
            tenant_id=tenant_id,
            agent_name=agent_name,
            input=dict(input),
            status=JobStatus.QUEUED,
            priority=priority,
            max_retries=max_retries,
            queued_at=now,
            created_at=now,
            updated_at=now,
        )

    # ---- 序列化 ----------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    def to_public(self) -> dict:
        """API 对外返回的字段（§8-10）。"""
        return {
            "job_id": self.id,
            "tenant_id": self.tenant_id,
            "agent": self.agent_name,
            "status": self.status.value,
            "priority": self.priority,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "current_step": self.current_step,
            "progress": self.progress,
            "worker_id": self.worker_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "queued_at": self.queued_at,
            "error": self.error,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        data = dict(d)
        data["status"] = JobStatus.from_str(data["status"])
        return cls(**data)

    # ---- 耗时指标 ----------------------------------------------------------

    @property
    def queue_wait_seconds(self) -> float | None:
        """Queue Wait：queued -> started（§47 必须与 Execution 分开统计）。"""
        if self.queued_at is None or self.started_at is None:
            return None
        return max(0.0, self.started_at - self.queued_at)

    @property
    def execution_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0.0, self.finished_at - self.started_at)

    @property
    def duration_seconds(self) -> float | None:
        if self.created_at is None or self.finished_at is None:
            return None
        return max(0.0, self.finished_at - self.created_at)
