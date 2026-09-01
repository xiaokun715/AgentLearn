"""单条流的生命周期：Producer 发布 -> 有界队列/Replay Buffer -> Consumer 消费。

关键设计（设计说明书 §20 / §21 / §26 / §42）：
  - Cancellation 必须沿调用链传播：Client 断开 -> cancel Producer 任务。
  - 部分失败：已开始流式输出后不能再返回 500，错误必须变成 Stream Event。
  - 这不是"把字符串 yield 出去"，而是可靠的事件流传输机制。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ..backpressure.controller import BackpressureController, BackpressureError
from ..buffer.base import ReplayBuffer
from ..config import StreamConfig
from ..provider.mock_llm import BaseProvider, ProviderError
from .event import StreamEvent
from .state import InvalidTransition, StreamState, StreamStatus


@dataclass(frozen=True, slots=True)
class StreamRequest:
    stream_id: str
    prompt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StreamResult:
    """部分结果状态（设计说明书 §27）。"""
    stream_id: str
    status: str
    content: str
    usage: dict[str, Any]
    error: dict[str, Any] | None = None
    ttft: float | None = None
    total_latency: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "stream_id": self.stream_id,
            "status": self.status,
            "content": self.content,
            "usage": self.usage,
        }
        if self.error is not None:
            d["error"] = self.error
        if self.ttft is not None:
            d["ttft"] = self.ttft
        if self.total_latency is not None:
            d["total_latency"] = self.total_latency
        return d


class Stream:
    """一条流的全部运行时状态与 Producer 任务编排。"""

    def __init__(
        self,
        request: StreamRequest,
        provider: BaseProvider,
        buffer: ReplayBuffer,
        backpressure: BackpressureController,
        config: StreamConfig,
        metrics,
    ):
        self.request = request
        self.provider = provider
        self.buffer = buffer
        self.backpressure = backpressure
        self.config = config
        self.metrics = metrics

        self.state = StreamState(stream_id=request.stream_id)
        self.producer: asyncio.Task | None = None
        self._producer_lock = asyncio.Lock()

        # 活跃消费者计数：本 Demo 采用"单消费者"模型（§10 Producer->Queue->Consumer），
        # 同一条流同一时刻只允许一个订阅者，避免事件被瓜分 / done 只达一人。
        self.consumers = 0
        self.consumer_lock = asyncio.Lock()

        # Provider 的位置游标（用于重连续传）与输出 token 累计
        self._token_index = 0
        self._output_tokens = 0
        self._content_parts: list[str] = []

        self.result: StreamResult | None = None

    # ------------------------------------------------------------------ #
    # 状态辅助
    # ------------------------------------------------------------------ #
    @property
    def stream_id(self) -> str:
        return self.request.stream_id

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def status(self) -> StreamStatus:
        return self.state.status

    def _transition_safe(self, new: StreamStatus, *, error_code: str | None = None) -> None:
        try:
            self.state.transition(new, error_code=error_code)
        except InvalidTransition:
            pass  # 幂等：重复取消等场景下忽略

    def _next_seq(self) -> int:
        self.state.last_seq += 1
        return self.state.last_seq

    # ------------------------------------------------------------------ #
    # Producer 编排
    # ------------------------------------------------------------------ #
    async def ensure_running(self) -> None:
        """确保 Producer 正在运行。断线后重连会在此处从上次位置恢复。"""
        async with self._producer_lock:
            if self.is_terminal:
                return
            if self.producer is not None and not self.producer.done():
                return  # 已在运行
            self.producer = asyncio.create_task(
                self._run_producer(), name=f"stream:{self.request.stream_id}"
            )

    async def _run_producer(self) -> None:
        """调用 Provider，把内容事件发布到 buffer + 有界队列。"""
        self._transition_safe(StreamStatus.RUNNING)
        iterator = self.provider.stream(self.request, start_token_index=self._token_index)
        try:
            while True:
                try:
                    pev = await asyncio.wait_for(
                        anext(iterator), timeout=self.config.provider_idle_timeout
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    # 上游长时间无事件（设计说明书 §26 Provider timeout）
                    raise ProviderError(
                        "UPSTREAM_TIMEOUT",
                        retryable=True,
                        detail=f"provider 在 {self.config.provider_idle_timeout}s 内未产出事件",
                    ) from None

                self._token_index += 1
                self._output_tokens += pev.output_tokens_inc
                if pev.type == "token":
                    self._content_parts.append(pev.data.get("delta", ""))

                event = StreamEvent(self.request.stream_id, self._next_seq(), pev.type, pev.data)
                await self._publish(event)

                if pev.type == "token" and self.state.first_token_at is None:
                    self.state.first_token_at = time.time()
                    self.metrics.record_first_token(self.request.stream_id, self.state)

            # 正常完成
            done_ev = StreamEvent(self.request.stream_id, self._next_seq(), "done", {"reason": "completed"})
            await self._publish_terminal(done_ev)
            self._transition_safe(StreamStatus.COMPLETED)
            self._finalize("completed")

        except asyncio.CancelledError:
            # Client 断开 -> 取消上游（设计说明书 §20）
            self._transition_safe(StreamStatus.CANCELLED)
            self._finalize("cancelled")
            raise

        except BackpressureError as exc:
            # 慢 Consumer 导致入队超时 -> 取消整条流
            # （backpressure 指标由 BackpressureController 在队列满时记录，这里不再重复计数）
            self._transition_safe(StreamStatus.CANCELLED, error_code=exc.code)
            done_ev = StreamEvent(self.request.stream_id, self._next_seq(), "done", {
                "reason": "cancelled",
                "error": {"code": exc.code, "retryable": True},
            })
            await self._publish_terminal(done_ev)
            self._finalize("cancelled", error={"code": exc.code, "retryable": True})

        except ProviderError as exc:
            # 部分失败：已经发出的 Token 不能丢，错误必须变成事件（设计说明书 §43 / §44）
            self._transition_safe(StreamStatus.FAILED, error_code=exc.code)
            err_ev = StreamEvent(self.request.stream_id, self._next_seq(), "error", exc.to_dict())
            await self._publish_terminal(err_ev)
            done_ev = StreamEvent(self.request.stream_id, self._next_seq(), "done", {"reason": "error"})
            await self._publish_terminal(done_ev)
            self._finalize("failed", error=exc.to_dict())

        except Exception as exc:  # 兜底：上游未知异常同样走事件通道
            code = "INTERNAL_ERROR"
            self._transition_safe(StreamStatus.FAILED, error_code=code)
            err_ev = StreamEvent(self.request.stream_id, self._next_seq(), "error", {
                "code": code, "retryable": False, "detail": str(exc),
            })
            await self._publish_terminal(err_ev)
            done_ev = StreamEvent(self.request.stream_id, self._next_seq(), "done", {"reason": "error"})
            await self._publish_terminal(done_ev)
            self._finalize("failed", error={"code": code, "retryable": False})
        finally:
            # 无论正常/异常/取消，都关闭 Provider 的 async generator，
            # 避免挂起的生成器泄漏其持有的外部资源（DB cursor / HTTP 连接等）。
            try:
                await iterator.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #
    async def _publish(self, event: StreamEvent) -> None:
        """普通事件：先落 Replay Buffer，再进有界队列（可能触发背压）。"""
        self.state.last_seq = event.seq
        await self.buffer.append(event)
        await self.backpressure.put(event)

    async def _publish_terminal(self, event: StreamEvent) -> None:
        """终止性事件（error/done）。

        优先参与背压（等消费者腾出空间），绝不挤占/丢弃尚未发送的业务 Token
        （设计说明书 §26 "已经发出的 Token 不能丢"）。只有消费者长时间不再消费
        （超过 max_queue_wait）这一极端情况下，才丢弃最旧事件保证终止必然到达。
        """
        self.state.last_seq = event.seq
        await self.buffer.append(event)
        try:
            await asyncio.wait_for(
                self.backpressure.queue.put(event), timeout=self.config.max_queue_wait
            )
        except asyncio.TimeoutError:
            try:
                self.backpressure.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.backpressure.queue.put_nowait(event)

    def _finalize(self, status: str, *, error: dict[str, Any] | None = None) -> None:
        """生成部分/最终 StreamResult 并记录指标（设计说明书 §27 / §31）。"""
        content = "".join(self._content_parts)
        usage = {
            "input_tokens": self.provider.input_tokens,
            "output_tokens": self._output_tokens,
        }
        self.result = StreamResult(
            stream_id=self.request.stream_id,
            status=status,
            content=content,
            usage=usage,
            error=error,
            ttft=self.state.ttft(),
            total_latency=self.state.total_latency(),
        )
        self.metrics.record_finalize(
            self.request.stream_id, self.state, status, self._output_tokens
        )

    # ------------------------------------------------------------------ #
    # 取消
    # ------------------------------------------------------------------ #
    async def cancel(self, reason: str = "client_disconnect") -> bool:
        """取消整条流并等待上游 Producer 真正停止。

        返回 True 表示确实取消了正在运行的流；False 表示流已结束/无需操作。
        """
        if self.is_terminal:
            return False
        active_before = self.state.status in (StreamStatus.CREATED, StreamStatus.RUNNING)
        if self.producer is not None and not self.producer.done():
            self.producer.cancel()
            try:
                await self.producer
            except BaseException:
                pass
        if not self.is_terminal:
            self._transition_safe(StreamStatus.CANCELLED)
            # 幂等：Producer 的异常处理器可能已经 _finalize 过（如取消/背压），
            # 这里只在结果尚未生成时 finalize，避免指标重复计数。
            if self.result is None:
                self._finalize("cancelled")
            if reason == "client_disconnect" and active_before:
                self.metrics.record_disconnect()
            return True
        return False
