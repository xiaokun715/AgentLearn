"""Redis 版 ConfigCache（可选，需 ``pip install prompt-config-registry[redis]``）。

Redis 的好处（§23）：
- 多实例共享：Resolver 部署多副本时缓存一致；
- 支持发布时主动失效（invalidate deployment key）；
- 版本化 key 天然不互相污染。
"""
from __future__ import annotations

from typing import Any

from .base import ConfigCache


class RedisConfigCache(ConfigCache):
    def __init__(self, url: str, prefix: str = "pcr:") -> None:
        self.url = url
        self.prefix = prefix
        self._redis: Any = None

    async def _client(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis  # 延迟导入：未安装 redis 时不报错

            self._redis = aioredis.from_url(self.url, decode_responses=True)
        return self._redis

    def _k(self, key: str) -> str:
        return self.prefix + key

    async def get(self, key: str) -> str | None:
        client = await self._client()
        return await client.get(self._k(key))

    async def set(self, key: str, value: str, ttl: int) -> None:
        client = await self._client()
        await client.set(self._k(key), value, ex=ttl)

    async def delete(self, *keys: str) -> int:
        client = await self._client()
        if not keys:
            return 0
        return await client.delete(*[self._k(k) for k in keys])

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
