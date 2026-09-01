"""StreamManager：流的注册表与 subscribe/replay 编排（设计说明书 §9 / §22）。

对外能力：
    create_stream / publish / subscribe / replay / cancel / close / disconnect
"""
from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from typing import Any, AsyncIterator, Callable, Optional

from ..backpressure.controller import BackpressureController
from ..buffer.base import ReplayBuffer, ResumeWindowExpired
from ..config import StreamConfig
from .event import StreamEvent
from .state import StreamStatus
from .stream import Stream, StreamRequest


class StreamNotFound(Exception):
    def __init__(self, stream_id: str):
        super().__init__(f"stream not found: {stream_id}")
        self.stream_id = stream_id


class StreamAlreadyExists(Exception):
    def __init__(self, stream_id: str):
        super().__init__(f"stream already exists: {stream_id}")
        self.stream_id = stream_id


class ConcurrentConsumer(Exception):
    """同一时刻已有活跃订阅者。单消费者模型（§10）下拒绝第二个订阅者。"""

    def __init__(self, stream_id: str):
        super().__init__(f"stream {stream_id} already has an active consumer")
        self.stream_id = stream_id


class StreamManager:
    def __init__(
        self,
        provider_factory: Callable[[str, str], Any],
        buffer_factory: Callable[[str], Any],
        config: StreamConfig,
        metrics,
    ):
        self._provider_factory = provider_factory   # (prompt, stream_id) -> BaseProvider
        self._buffer_factory = buffer_factory       # (stream_id) -> ReplayBuffer
        self.config = config
        self.metrics = metrics
        self._streams: dict[str, Stream] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get(self, stream_id: str) -> Optional[Stream]:
        return self._streams.get(stream_id)

    def exists(self, stream_id: str) -> bool:
        return stream_id in self._streams

    def count(self) -> int:
        return len(self._streams)

    def result(self, stream_id: str) -> Optional[Any]:
        stream = self.get(stream_id)
        return stream.result if stream is not None else None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def create_stream(
        self, prompt: str = "", *, stream_id: str | None = None, metadata: dict[str, Any] | None = None
    ) -> str:
        async with self._lock:
            sid = stream_id or uuid.uuid4().hex
            if sid in self._streams:
                raise StreamAlreadyExists(sid)
            request = StreamRequest(stream_id=sid, prompt=prompt, metadata=metadata or {})
            provider = self._provider_factory(prompt=prompt, stream_id=sid)
            buffer = await self._buffer_factory(sid)
            queue = asyncio.Queue(maxsize=self.config.queue_size)
            backpressure = BackpressureController(
                sid,
                queue,
                strategy=self.config.backpressure_strategy,
                max_queue_wait=self.config.max_queue_wait,
                metrics=self.metrics,
            )
            stream = Stream(request, provider, buffer, backpressure, self.config, self.metrics)
            self._streams[sid] = stream
            self.metrics.record_request(sid)
            return sid

    async def publish(self, stream_id: str, event: StreamEvent) -> None:
        """供外部 Producer 向已存在流发布事件（本 Demo 主要由 Stream 内部调用）。"""
        stream = self.get(stream_id)
        if stream is None:
            raise StreamNotFound(stream_id)
        event.stream_id = stream_id
        if event.seq <= 0:
            event.seq = stream._next_seq()
        await stream._publish(event)

    # ------------------------------------------------------------------ #
    # 订阅：重放 + 实时（设计说明书 §22 / §24）
    # ------------------------------------------------------------------ #
    async def subscribe(
        self,
        stream_id: str,
        *,
        last_seq: int = 0,
        probe: Callable[[], Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """订阅一条流。

        last_seq：客户端已收到的最大 seq（Last-Event-ID）。
        probe：可选的断线探测回调，返回 True 表示消费者已断开。

        流程：
          1) 校验 Replay Window；若 last_seq 已过期 -> ResumeWindowExpired（409）。
          2) 确保 Producer 运行（断线后自动恢复）。
          3) 从 buffer 重放 seq > last_seq 的事件。
          4) 转入实时消费有界队列；超时无事件时按心跳间隔发 heartbeat。
        """
        stream = self.get(stream_id)
        if stream is None:
            raise StreamNotFound(stream_id)

        # 单消费者模型：同一时刻只允许一个订阅者，避免事件被瓜分 / done 只达一人
        async with stream.consumer_lock:
            if stream.consumers > 0:
                raise ConcurrentConsumer(stream_id)
            stream.consumers += 1
        try:
            async for ev in self._subscribe_iter(stream, last_seq, probe):
                yield ev
        finally:
            stream.consumers -= 1

    async def _subscribe_iter(self, stream, last_seq: int, probe):
        """subscribe 的实际迭代体（被消费者计数包裹）。"""
        if last_seq > 0:
            await stream.buffer.validate_replay_window(stream.stream_id, last_seq)
            self.metrics.record_reconnect()
            self.metrics.record_replay()

        await stream.ensure_running()

        replayed = await stream.buffer.replay(last_seq)
        delivered = replayed[-1].seq if replayed else last_seq
        for ev in replayed:
            yield ev

        if stream.is_terminal:
            return

        last_event_at = time.monotonic()
        while True:
            try:
                ev = await asyncio.wait_for(
                    stream.backpressure.get(), timeout=self.config.poll_interval
                )
                last_event_at = time.monotonic()
            except asyncio.TimeoutError:
                if probe is not None:
                    res = probe()
                    if inspect.isawaitable(res):
                        res = await res
                    if res:
                        return  # 消费者已断开
                if time.monotonic() - last_event_at >= self.config.heartbeat_interval:
                    # 长时间无事件，发送心跳保持中间层连接（设计说明书 §16）
                    yield StreamEvent(stream.stream_id, delivered, "heartbeat", {})
                    last_event_at = time.monotonic()
                continue

            # done 优先于去重判断：保证流必然终止，绝不因 seq 游标而跳过 done
            if ev.type == "done":
                yield ev
                return
            if ev.seq <= delivered:
                continue  # 重放与实时重叠，按 seq 去重
            delivered = ev.seq
            yield ev

    # ------------------------------------------------------------------ #
    # 取消 / 关闭
    # ------------------------------------------------------------------ #
    async def cancel(self, stream_id: str, *, reason: str = "client_cancel") -> None:
        stream = self.get(stream_id)
        if stream is None:
            return
        await stream.cancel(reason=reason)

    async def disconnect(self, stream_id: str) -> None:
        """Client 断开：取消下游 -> 取消上游 LLM -> finalize usage（设计说明书 §20）。

        只有当前没有活跃订阅者时才取消上游；否则（例如旧连接的 finally 与新连接
        并发）不能误杀新订阅者刚刚恢复的 Producer。
        """
        stream = self.get(stream_id)
        if stream is None:
            return
        if stream.consumers == 0:
            await stream.cancel(reason="client_disconnect")

    async def close(self, stream_id: str) -> None:
        stream = self._streams.pop(stream_id, None)
        if stream is None:
            return
        await stream.cancel(reason="close")
        stream._transition_safe(StreamStatus.CLOSED)
        # 释放后端资源（如 Redis 连接）
        closer = getattr(stream.buffer, "close", None)
        if closer is not None:
            await closer()

    async def close_all(self) -> None:
        for sid in list(self._streams):
            await self.close(sid)
