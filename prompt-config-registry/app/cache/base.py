"""ConfigCache 接口 —— 存 JSON 字符串，带 TTL，支持按 key 删除（失效）。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ConfigCache(ABC):
    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def set(self, key: str, value: str, ttl: int) -> None: ...

    @abstractmethod
    async def delete(self, *keys: str) -> int: ...

    async def close(self) -> None:
        return None
