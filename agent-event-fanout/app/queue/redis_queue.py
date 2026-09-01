"""Redis 队列 —— 可选后端（需 QUEUE_BACKEND=redis + REDIS_URL）。

使用 Redis List：``RPUSH`` 入队 + ``BLPOP`` 出队（带超时）。
"""
from __future__ import annotations

from .base import EventQueue


class RedisQueue(EventQueue):
    def __init__(self, name: str, redis_url: str) -> None:
        self.name = name
        self.redis_url = redis_url
        self._key = f"fanout:{name}"
        self._client = None

    def _r(self):
        import redis.asyncio as aioredis

        if self._client is None:
            self._client = aioredis.from_url(self.redis_url)
        return self._client

    async def publish(self, payload: str) -> None:
        await self._r().rpush(self._key, payload)

    async def pop(self, timeout: float = 0.1) -> str | None:
        result = await self._r().blpop(self._key, timeout=timeout)
        if result is None:
            return None
        _, value = result
        return value.decode()

    async def size(self) -> int:
        return await self._r().llen(self._key)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
