"""内存版 Replay Buffer：deque(maxlen=N) 实现（设计说明书 §23 / §24 / §25）。"""
from __future__ import annotations

from collections import deque
from typing import List

from ..core.event import StreamEvent
from .base import ReplayBuffer


class MemoryReplayBuffer(ReplayBuffer):
    """用定长 deque 保存最近 max_events 个事件。

    为什么是定长？因为不能无限缓存 Streaming Event（设计说明书 §12 / §48 Memory）。
    """

    def __init__(self, max_events: int = 1000):
        self.max_events = max_events
        self._deque: deque[StreamEvent] = deque(maxlen=max_events)

    async def append(self, event: StreamEvent) -> None:
        self._deque.append(event)

    async def events(self) -> List[StreamEvent]:
        return list(self._deque)

    async def replay(self, last_seq: int) -> List[StreamEvent]:
        return [e for e in self._deque if e.seq > last_seq]

    async def oldest_seq(self) -> int:
        return self._deque[0].seq if self._deque else 0

    async def last_seq(self) -> int:
        return self._deque[-1].seq if self._deque else 0
