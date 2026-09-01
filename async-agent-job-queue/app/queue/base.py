"""JobQueue 接口（设计说明书 §11-12）。

为什么不能直接用 asyncio.Queue？因为进程崩溃后队列内数据全部丢失（§12）。
真实项目用 Redis Streams / RabbitMQ / Kafka / SQS / PG Queue。
这里抽象出统一接口：内存实现（默认）+ Redis Streams 实现（可选）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class JobQueue(ABC):
    @abstractmethod
    async def publish(
        self, job_id: str, *, priority: int = 0, tenant: str = "default"
    ) -> None:
        """入队一个 Job ID。"""

    @abstractmethod
    async def get(self) -> str:
        """阻塞取出一个 Job ID（Worker 消费端）。"""

    @abstractmethod
    async def ack(self, job_id: str) -> None:
        """确认消费完成（内存队列为 no-op，Redis 中 XACK）。"""

    @abstractmethod
    async def close(self) -> None:
        ...

    @abstractmethod
    def depth(self) -> int:
        """当前队列深度（指标 agent_queue_depth 用）。"""
