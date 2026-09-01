"""内存队列 —— 默认实现（零外部依赖，测试友好）。"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Deque

from .base import EventQueue


class MemoryQueue(EventQueue):
    def __init__(self, name: str) -> None:
        self.name = name
        self._q: Deque[str] = deque()
        self._cond = asyncio.Condition()

    async def publish(self, payload: str) -> None:
        async with self._cond:
            self._q.append(payload)
            self._cond.notify_all()

    async def pop(self, timeout: float = 0.1) -> str | None:
        async with self._cond:
            deadline = asyncio.get_event_loop().time() + timeout
            while not self._q:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None
            return self._q.popleft()

    async def size(self) -> int:
        return len(self._q)
