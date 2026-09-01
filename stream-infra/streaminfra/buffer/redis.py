"""Redis Stream 版 Replay Buffer（设计说明书 §36 / §37）。

为什么用 Redis Stream 而不是 Pub/Sub？
  Pub/Sub 在 Consumer 断线时消息直接丢失；Redis Stream 会保留消息，
  因此需要 Resume / Replay 时 Stream 比 Pub/Sub 更合适。

本实现以事件的 seq 作为 Redis Stream 的条目 ID（如 "4-0"），
从而 XRANGE '(last_seq +' 天然支持按 seq 重放。live 订阅仍走内存有界队列，
Redis 缓冲用于跨进程/重启的持久重放。

Redis 为可选依赖：未安装时导入不会失败，仅在使用时抛错。
"""
from __future__ import annotations

import json
from typing import Any, List

from ..core.event import StreamEvent
from .base import ReplayBuffer

try:  # 可选依赖：pip install streaminfra[redis]
    import redis.asyncio as aioredis

    _REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    aioredis = None
    _REDIS_AVAILABLE = False


def _as_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class RedisReplayBuffer(ReplayBuffer):
    def __init__(self, stream_id: str, url: str = "redis://localhost:6379/0", max_events: int = 1000):
        if not _REDIS_AVAILABLE:
            raise RuntimeError("需要 redis 依赖：pip install streaminfra[redis]")
        self.stream_id = stream_id
        self.key = f"stream:{stream_id}"
        self.max_events = max_events
        self._redis = aioredis.Redis.from_url(url)

    async def close(self) -> None:
        await self._redis.aclose()

    async def append(self, event: StreamEvent) -> None:
        await self._redis.xadd(
            self.key,
            {"type": _as_str(event.type), "data": json.dumps(event.data, ensure_ascii=False)},
            id=f"{event.seq}-0",
            maxlen=self.max_events,
        )

    async def events(self) -> List[StreamEvent]:
        rows = await self._redis.xrange(self.key, min="-", max="+")
        return [self._decode(rid, fields) for rid, fields in rows]

    async def replay(self, last_seq: int) -> List[StreamEvent]:
        if last_seq >= await self.last_seq():
            return []
        rows = await self._redis.xrange(self.key, min=f"({last_seq}", max="+")
        return [self._decode(rid, fields) for rid, fields in rows]

    async def oldest_seq(self) -> int:
        rows = await self._redis.xrange(self.key, min="-", max="+", count=1)
        if not rows:
            return 0
        return int(rows[0][0].split("-")[0])

    async def last_seq(self) -> int:
        rows = await self._redis.xrevrange(self.key, max="+", min="-", count=1)
        if not rows:
            return 0
        return int(rows[0][0].split("-")[0])

    def _decode(self, rid: str, fields: dict) -> StreamEvent:
        return StreamEvent(
            stream_id=self.stream_id,
            seq=int(rid.split("-")[0]),
            type=_as_str(fields["type"]),
            data=json.loads(_as_str(fields["data"])),
        )
