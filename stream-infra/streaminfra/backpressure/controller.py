"""BackpressureController：有界队列 + 背压策略（设计说明书 §11 / §13）。

核心：LLM(Producer, 快) -> 有界 Queue -> Client(Consumer, 慢)
当 Queue 满了之后，Producer 调用 await put() 会被阻塞，这就是 Backpressure。

为什么不能无限 Queue？LLM 1000 token/s、Client 10 token/s 时，
每秒堆积 990 条，内存不断上涨最终 OOM（设计说明书 §12）。

策略（设计说明书 §13）：
    BLOCK        阻塞 Producer（不丢 Token，Demo 推荐）
    DROP_OLDEST  丢弃最旧事件（不适合 LLM Token）
    DROP_NEWEST  丢弃最新事件（不适合 LLM Token）
    DISCONNECT   断开慢 Consumer（适合高并发系统保护）

Demo 额外实现 max_queue_wait：Producer 入队阻塞超过该时长则抛出
BackpressureTimeout，由上层 cancel 整条流。
"""
from __future__ import annotations

import asyncio

from ..config import BackpressureStrategy


class BackpressureError(Exception):
    """背压相关异常的基类。"""

    def __init__(self, stream_id: str, *, code: str = "backpressure_timeout", timeout: float | None = None):
        super().__init__(f"stream {stream_id} backpressure error: {code}")
        self.stream_id = stream_id
        self.code = code
        self.timeout = timeout


class BackpressureTimeout(BackpressureError):
    """Producer 入队等待超过 max_queue_wait -> 应取消整条流。"""

    def __init__(self, stream_id: str, timeout: float):
        super().__init__(stream_id, code="backpressure_timeout", timeout=timeout)


class BackpressureTooSlow(BackpressureError):
    """策略 D：队列已满且启用 DISCONNECT -> 慢 Consumer 应被断开。"""

    def __init__(self, stream_id: str):
        super().__init__(stream_id, code="client_too_slow")


class BackpressureController:
    def __init__(
        self,
        stream_id: str,
        queue: asyncio.Queue,
        strategy: BackpressureStrategy = BackpressureStrategy.BLOCK,
        max_queue_wait: float = 30.0,
        metrics=None,
    ):
        self.stream_id = stream_id
        self.queue = queue
        self.strategy = strategy
        self.max_queue_wait = max_queue_wait
        self.metrics = metrics
        self.wait_count = 0  # Producer 因队列已满而等待的次数

    @property
    def full(self) -> bool:
        return self.queue.full()

    @property
    def size(self) -> int:
        return self.queue.qsize()

    def _record_throttle(self) -> None:
        self.wait_count += 1
        if self.metrics is not None:
            self.metrics.record_backpressure()

    async def put(self, event) -> None:
        """Producer 入队。队列满时按 strategy 处理。"""
        if self.queue.full():
            self._record_throttle()
            if self.strategy == BackpressureStrategy.DROP_OLDEST:
                try:
                    self.queue.get_nowait()  # 丢掉最旧，给新事件腾位置
                except asyncio.QueueEmpty:
                    pass
            elif self.strategy == BackpressureStrategy.DROP_NEWEST:
                return  # 丢掉新事件
            elif self.strategy == BackpressureStrategy.DISCONNECT:
                raise BackpressureTooSlow(self.stream_id)
            # BLOCK / DROP_OLDEST 继续走到下面的阻塞 put
        try:
            await asyncio.wait_for(self.queue.put(event), timeout=self.max_queue_wait)
        except asyncio.TimeoutError as exc:
            raise BackpressureTimeout(self.stream_id, self.max_queue_wait) from exc

    async def get(self):
        """Consumer 出队。"""
        return await self.queue.get()
