"""集中管理 Job 状态迁移（设计说明书 §6 / §7）。

为什么不能到处 `job.status = "running"`？
因为真实运行可能出现 Worker A / Worker B 并发接管同一个 Job，
如果状态没有严格定义，RUNNING / SUCCESS / RETRYING 会互相覆盖。

因此所有状态变更都必须经过这里校验（存储层再通过条件 UPDATE 原子兜底）。
"""
from __future__ import annotations

from .status import JobStatus


class InvalidTransitionError(Exception):
    """非法的状态迁移。"""

    def __init__(self, frm: JobStatus, to: JobStatus) -> None:
        self.frm = frm
        self.to = to
        super().__init__(f"invalid job transition: {frm.value} -> {to.value}")


class JobStateMachine:
    """状态机：定义合法迁移边，并提供校验入口。

    迁移图（§6）：

        QUEUED ──► RUNNING ──┬──► SUCCESS
                 │           ├──► RETRYING ──► QUEUED/RUNNING
                 │           ├──► FAILED ──► DEAD
                 ▼           └──► CANCELLED
              CANCELLED

    额外说明：
    - RUNNING/RETRYING ──► QUEUED：Lease 过期后由 Reaper 重新入队（故障恢复，§30-31）。
    - DEAD ──► QUEUED：人工从 DLQ 重新入队（§28）。
    """

    # from: set of allowed next statuses
    _TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
        JobStatus.QUEUED: {
            JobStatus.RUNNING,
            JobStatus.CANCELLED,
        },
        JobStatus.RUNNING: {
            JobStatus.SUCCESS,
            JobStatus.FAILED,
            JobStatus.RETRYING,
            JobStatus.CANCELLED,
            JobStatus.QUEUED,  # lease 过期 -> Reaper 重新入队
        },
        JobStatus.RETRYING: {
            JobStatus.QUEUED,  # backoff 结束，重新入队
            JobStatus.RUNNING,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.DEAD,  # 重试次数用尽
        },
        JobStatus.FAILED: {
            JobStatus.DEAD,  # 进入 DLQ
        },
        JobStatus.DEAD: {
            JobStatus.QUEUED,  # 人工重投
        },
        JobStatus.SUCCESS: set(),
        JobStatus.CANCELLED: set(),
    }

    @classmethod
    def can_transition(cls, frm: JobStatus, to: JobStatus) -> bool:
        return to in cls._TRANSITIONS.get(frm, set())

    @classmethod
    def assert_can_transition(cls, frm: JobStatus, to: JobStatus) -> None:
        if not cls.can_transition(frm, to):
            raise InvalidTransitionError(frm, to)

    @classmethod
    def is_terminal(cls, status: JobStatus) -> bool:
        return not cls._TRANSITIONS.get(status)
