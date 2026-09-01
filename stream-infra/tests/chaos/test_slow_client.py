"""慢客户端（设计说明书 §12 / §13 / §31 Backpressure）。

Producer 快、Consumer 慢 -> 有界队列打满 -> 阻塞 Producer ->
超过 max_queue_wait -> 取消整条流并记 backpressure 指标。

在管理器层面直接模拟"Producer 快、Consumer 慢"：启动 Producer 但不消费，
队列打满后 Producer 被阻塞，等待超过 max_queue_wait 触发取消。

注：在真实 HTTP 层很难稳定复现该场景——响应体较小时会先被 TCP 发送缓冲吸收，
慢客户端并不会真正反向阻塞到 Producer。这也说明"有界队列 + 阻塞 Producer"
保护的是服务端内存，而不是 TCP 层。
"""
import asyncio

import pytest

from streaminfra.config import StreamConfig
from streaminfra.core.state import StreamStatus
from streaminfra.main import create_app


@pytest.mark.asyncio
async def test_slow_consumer_backpressure():
    config = StreamConfig(
        queue_size=2,
        max_queue_wait=0.1,
        provider_delay=0.001,   # Producer 非常快
        poll_interval=0.05,
        mock_tokens=["t"] * 50,
    )
    app = create_app(config=config)
    mgr = app.state.stream_manager
    sid = await mgr.create_stream(prompt="x")
    stream = mgr.get(sid)

    # 启动 Producer，但 Consumer 不读取 -> 队列打满 -> Producer 阻塞 -> 超时
    await stream.ensure_running()
    if stream.producer is not None:
        try:
            await asyncio.wait_for(stream.producer, timeout=2.0)
        except BaseException:
            pass

    assert app.state.metrics.stream_backpressure_total > 0
    assert stream.status == StreamStatus.CANCELLED
    assert stream.state.error_code == "backpressure_timeout"
    assert stream.result is not None
    assert stream.result.status == "cancelled"


@pytest.mark.asyncio
async def test_slow_consumer_with_slow_reader_also_blocks():
    """Consumer 读取慢到超过 max_queue_wait 时，Producer 会被背压超时取消。"""
    config = StreamConfig(
        queue_size=2,
        max_queue_wait=0.05,   # 比消费者的读取间隔更短，保证触发超时
        provider_delay=0.001,
        poll_interval=0.05,
        mock_tokens=["t"] * 20,
    )
    app = create_app(config=config)
    mgr = app.state.stream_manager
    sid = await mgr.create_stream(prompt="x")
    stream = mgr.get(sid)

    consumed = 0
    sub = mgr.subscribe(sid)
    async for ev in sub:
        if ev.type == "token":
            consumed += 1
            await asyncio.sleep(0.1)  # 每取一个 token 都刻意放慢（> max_queue_wait）
        if ev.type == "done":
            break
    await sub.aclose()

    assert app.state.metrics.stream_backpressure_total > 0
    assert consumed < 20, "慢消费者不应消费完所有 token"
    assert stream.status == StreamStatus.CANCELLED
    assert stream.state.error_code == "backpressure_timeout"
