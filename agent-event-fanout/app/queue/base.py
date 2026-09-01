"""队列抽象（设计说明书 §25, §33, §34）。

Redis 用来模拟 Queue（§29）。测试/本地可用内存实现。
payload 为字符串（event_id 或 delivery_id）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class EventQueue(ABC):
    name: str

    @abstractmethod
    async def publish(self, payload: str) -> None:
        """入队。"""

    @abstractmethod
    async def pop(self, timeout: float = 0.1) -> str | None:
        """出队；队列为空时最多等 ``timeout`` 秒，超时返回 None。"""

    @abstractmethod
    async def size(self) -> int:
        """当前队列长度（调试/测试用）。"""

    async def close(self) -> None:  # noqa: B027
        return None
