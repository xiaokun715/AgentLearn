"""JobStore 接口（设计说明书 §32）。

所有状态变更都通过 ``transition`` 完成 —— 它在存储层做 **条件更新**
（WHERE status = 期望的旧状态），配合 domain 层 JobStateMachine，
从根上杜绝「两个 Worker 互相覆盖状态」的问题。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..domain.job import Job
from ..domain.status import JobStatus


class JobStore(ABC):
    """Job 的持久化存储。"""

    # ---- 基础 CRUD --------------------------------------------------------

    @abstractmethod
    async def create(self, job: Job) -> Job:
        """写入一个新 Job。"""

    @abstractmethod
    async def get(self, job_id: str) -> Job | None:
        ...

    @abstractmethod
    async def update(self, job: Job) -> Job:
        """整行更新（幂等写）。"""

    # ---- 状态迁移（原子） --------------------------------------------------

    @abstractmethod
    async def transition(
        self,
        job_id: str,
        from_status: JobStatus,
        to_status: JobStatus,
        **fields: Any,
    ) -> bool:
        """仅当当前 status == from_status 时迁移到 to_status，返回是否成功。

        其余字段（worker_id / error / result / ...）通过 kwargs 一并原子写入。
        必须使用数据库条件 UPDATE（WHERE status = from_status）。
        """

    # ---- Lease / 故障恢复（§29-31） -----------------------------------------

    @abstractmethod
    async def acquire_lease(self, job_id: str, worker_id: str, duration: float) -> bool:
        """原子获取租约。

        规则：job 处于 QUEUED/RETRYING，或租约已过期（可被接管），或
        worker_id 已是自己（续约），才能拿到租约。
        """

    @abstractmethod
    async def renew_lease(self, job_id: str, worker_id: str, duration: float) -> bool:
        """Heartbeat：刷新 lease_expire_at。"""

    @abstractmethod
    async def release_lease(self, job_id: str, worker_id: str) -> bool:
        ...

    @abstractmethod
    async def expire_lease(self, job_id: str, worker_id: str) -> bool:
        """让租约立即过期（Worker 崩溃时调用，加速 Reaper 接管，§31）。"""

    @abstractmethod
    async def update_progress(self, job_id: str, worker_id: str, *, step: str | None, progress: int) -> bool:
        """RUNNING 中更新 current_step / progress（需校验 worker 仍是持有者）。"""

    @abstractmethod
    async def find_recoverable(self, now: float, grace: float) -> list[Job]:
        """扫描 RUNNING/RETRYING 且租约已过期（含 grace）的 Job —— Reaper 用。"""

    # ---- 取消（§10） ---------------------------------------------------------

    @abstractmethod
    async def set_cancel_requested(self, job_id: str, value: bool = True) -> bool:
        """置位取消信号（RUNNING 中协作式取消）。"""

    @abstractmethod
    async def is_cancel_requested(self, job_id: str) -> bool:
        ...

    # ---- 查询 ---------------------------------------------------------------

    @abstractmethod
    async def list_dead(self) -> list[Job]:
        """DLQ 中的 Job（status == DEAD）。"""

    @abstractmethod
    async def count_by_status(self) -> dict[str, int]:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...
