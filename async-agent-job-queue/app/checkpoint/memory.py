"""内存版 CheckpointStore —— 单进程 Demo 用。"""
from __future__ import annotations

from typing import Any

from .base import CheckpointStore


class MemoryCheckpointStore(CheckpointStore):
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    async def save(self, job_id: str, checkpoint: dict) -> None:
        self._data[job_id] = checkpoint

    async def load(self, job_id: str) -> dict | None:
        return self._data.get(job_id)

    async def delete(self, job_id: str) -> None:
        self._data.pop(job_id, None)

    async def close(self) -> None:
        return None
