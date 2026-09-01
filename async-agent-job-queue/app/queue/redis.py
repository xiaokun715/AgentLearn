"""Redis Streams 版 JobQueue（可选后端，设计说明书 §12/§13 V2）。

使用 Consumer Group 实现「多条消息投递 + 至少一次 + Ack」语义：
- publish      -> XADD（消息体即 job_id）
- get          -> XREADGROUP（消费组拉取，阻塞 5s 重试）
- ack          -> XACK

需要 ``pip install redis`` 与运行中的 Redis（见 docker-compose.yml）。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .base import JobQueue


class RedisStreamsQueue(JobQueue):
    STREAM = "agent_jobs"
    GROUP = "agent_workers"

    def __init__(self, redis_url: str = "redis://localhost:6379/0") -> None:
        self._redis_url = redis_url
        self._client: Any = None
        self._consumer = f"worker-{uuid.uuid4().hex[:8]}"
        self._pending: dict[str, str] = {}  # job_id -> stream message id（用于 ack）

    async def _ensure(self) -> None:
        if self._client is not None:
            return
        from redis.asyncio import Redis  # 延迟导入

        self._client = Redis.from_url(self._redis_url)
        # 创建消费组（EX = 不存在则建）
        try:
            await self._client.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
        except Exception:  # noqa: BLE001 —— BUSYGROUP 说明已存在
            pass

    async def publish(
        self, job_id: str, *, priority: int = 0, tenant: str = "default"
    ) -> None:
        await self._ensure()
        await self._client.xadd(self.STREAM, {"job_id": job_id})

    async def get(self) -> str:
        await self._ensure()
        while True:
            try:
                resp = await self._client.xreadgroup(
                    self.GROUP, self._consumer, {self.STREAM: ">"},
                    count=1, block=5000,
                )
            except Exception:  # noqa: BLE001  Redis 连接抖动时重试
                await asyncio.sleep(0.5)
                continue
            if resp:
                stream, messages = resp[0]
                msg_id, fields = messages[0]
                job_id = fields[b"job_id"].decode() if isinstance(fields[b"job_id"], bytes) else fields["job_id"]
                self._pending[job_id] = msg_id
                return job_id

    async def ack(self, job_id: str) -> None:
        msg_id = self._pending.pop(job_id, None)
        if msg_id is None or self._client is None:
            return
        await self._client.xack(self.STREAM, self.GROUP, msg_id)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def depth(self) -> int:
        return 0  # Redis 端长度需 XLEN；内存态不缓存，返回 0 以保持接口一致
