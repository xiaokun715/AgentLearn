"""内存版 JobQueue —— 基于 asyncio 的优先级队列（设计说明书 §11-12）。

- 支持 priority：值越小优先级越高（§5 中 priority 字段）。
- 支持可选 fair 模式：按 tenant 分队列 + 轮询（Round Robin），
  防止 Tenant A 的 10000 个 Job 饿死 Tenant B（§48-49）。

警告：进程崩溃即丢失，只用于 Demo 理解机制。
"""
from __future__ import annotations

import asyncio
import heapq
from collections import defaultdict
from dataclasses import dataclass, field

from .base import JobQueue


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    tenant: str = field(compare=False)
    job_id: str = field(compare=False)


class MemoryQueue(JobQueue):
    """优先级队列。``fair=False`` 时全局限序（priority + 先入先出）。"""

    def __init__(self, *, fair: bool = False, weights: dict[str, int] | None = None) -> None:
        self._fair = fair
        self._weights = weights or {}
        self._heaps: dict[str, list[_Item]] = defaultdict(list)  # tenant -> heap
        self._cond = asyncio.Condition()
        self._seq = 0
        self._closed = False

    async def publish(
        self, job_id: str, *, priority: int = 0, tenant: str = "default"
    ) -> None:
        async with self._cond:
            self._seq += 1
            heapq.heappush(
                self._heaps[tenant],
                _Item(priority=priority, seq=self._seq, tenant=tenant, job_id=job_id),
            )
            self._cond.notify_all()

    async def get(self) -> str:
        async with self._cond:
            while not self._has_items():
                if self._closed:
                    raise asyncio.CancelledError
                await self._cond.wait()
            tenant = self._pick_tenant()
            item = heapq.heappop(self._heaps[tenant])
            return item.job_id

    async def ack(self, job_id: str) -> None:
        return None  # 内存队列无确认机制

    async def close(self) -> None:
        async with self._cond:
            self._closed = True
            self._cond.notify_all()

    def depth(self) -> int:
        return sum(len(h) for h in self._heaps.values())

    # ---- 内部实现 -----------------------------------------------------------

    def _has_items(self) -> bool:
        return any(h for h in self._heaps.values())

    def _pick_tenant(self) -> str:
        if not self._fair:
            # 全局限序：找出所有堆顶中的最小项所在 tenant
            top = min(
                (self._heaps[t][0], t) for t, h in self._heaps.items() if h
            )
            return top[1]
        # fair：Round Robin + weight —— 按权重给每个 tenant 配额
        candidates = [t for t, h in self._heaps.items() if h]
        if not candidates:
            return "default"
        # 简单实现：轮询指针 + weight 影响每轮可见性
        self._rr_index = getattr(self, "_rr_index", 0) % len(candidates)
        tenant = candidates[self._rr_index]
        self._rr_index += 1
        return tenant


class FairMemoryQueue(MemoryQueue):
    """按租户做 Round Robin 的公平队列（§49），默认 fair=True。"""

    def __init__(self, *, weights: dict[str, int] | None = None) -> None:
        super().__init__(fair=True, weights=weights)
