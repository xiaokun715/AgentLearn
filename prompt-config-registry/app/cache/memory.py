"""内存版 ConfigCache —— 零依赖，测试 / Demo 默认使用。

惰性过期：get 时检查 TTL，过期即视为 miss 并清理。
"""
from __future__ import annotations

import time

from .base import ConfigCache


class MemoryConfigCache(ConfigCache):
    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._expire: dict[str, float] = {}

    async def get(self, key: str) -> str | None:
        expire_at = self._expire.get(key)
        if expire_at is not None and expire_at < time.monotonic():
            self._store.pop(key, None)
            self._expire.pop(key, None)
            return None
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._store[key] = value
        self._expire[key] = time.monotonic() + ttl

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if self._store.pop(key, None) is not None:
                self._expire.pop(key, None)
                deleted += 1
        return deleted

    async def close(self) -> None:
        self._store.clear()
        self._expire.clear()
