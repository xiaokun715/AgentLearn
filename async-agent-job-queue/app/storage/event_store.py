"""EventStore 接口（设计说明书 §34）。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..domain.events import JobEvent


class EventStore(ABC):
    """append-only 的事件存储。"""

    @abstractmethod
    async def append(self, job_id: str, event_type: str, payload: dict | None = None) -> JobEvent:
        ...

    @abstractmethod
    async def list(self, job_id: str) -> list[JobEvent]:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...
