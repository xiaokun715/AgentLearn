"""进程内取消状态存储 —— 用 dict 模拟 Redis 的 ``task_status:{task_id} -> cancelled``。"""
from __future__ import annotations

from .base import CancellationStore


class MemoryCancellationStore(CancellationStore):
    def __init__(self) -> None:
        self._flags: dict[str, bool] = {}

    def is_cancelled(self, task_id: str) -> bool:
        return self._flags.get(task_id, False)

    def mark_cancelled(self, task_id: str) -> None:
        self._flags[task_id] = True

    def clear(self, task_id: str) -> None:
        self._flags.pop(task_id, None)
