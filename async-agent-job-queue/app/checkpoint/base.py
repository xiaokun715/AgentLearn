"""CheckpointStore 接口（设计说明书 §19）。

Checkpoint 是「Agent 在某个安全恢复点的完整执行状态」。
保存内容包括 completed_steps 与 tool_records（§23 幂等依据）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class CheckpointStore(ABC):
    @abstractmethod
    async def save(self, job_id: str, checkpoint: dict) -> None:
        """保存（覆盖）一个 Job 的最新检查点。checkpoint 为 JSON 兼容 dict。"""

    @abstractmethod
    async def load(self, job_id: str) -> dict | None:
        ...

    @abstractmethod
    async def delete(self, job_id: str) -> None:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...
