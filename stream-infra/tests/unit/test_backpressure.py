"""背压单测（设计说明书 §11 / §12 / §13）。"""
import asyncio

import pytest

from streaminfra.backpressure.controller import (
    BackpressureController,
    BackpressureStrategy,
    BackpressureTimeout,
    BackpressureTooSlow,
)
from streaminfra.core.event import StreamEvent


def ev(seq: int) -> StreamEvent:
    return StreamEvent(stream_id="s", seq=seq, type="token", data={"delta": "x"})


@pytest.mark.asyncio
async def test_block_strategy_timeout():
    """队列满 -> Producer 阻塞 -> 超过 max_queue_wait 抛超时。"""
    q = asyncio.Queue(maxsize=2)
    ctrl = BackpressureController("s", q, strategy=BackpressureStrategy.BLOCK, max_queue_wait=0.05)
    await ctrl.put(ev(1))
    await ctrl.put(ev(2))
    assert ctrl.full
    with pytest.raises(BackpressureTimeout):
        await ctrl.put(ev(3))
    assert ctrl.wait_count >= 1


@pytest.mark.asyncio
async def test_block_strategy_proceeds_after_consumer_drains():
    """Consumer 取走事件后，阻塞的 Producer 得以继续。"""
    q = asyncio.Queue(maxsize=1)
    ctrl = BackpressureController("s", q, strategy=BackpressureStrategy.BLOCK, max_queue_wait=2.0)
    await ctrl.put(ev(1))
    assert (await ctrl.get()).seq == 1
    await ctrl.put(ev(2))
    assert q.qsize() == 1


@pytest.mark.asyncio
async def test_drop_oldest():
    """策略 B：队列满时丢弃最旧。"""
    q = asyncio.Queue(maxsize=2)
    ctrl = BackpressureController("s", q, strategy=BackpressureStrategy.DROP_OLDEST, max_queue_wait=0.05)
    await ctrl.put(ev(1))
    await ctrl.put(ev(2))
    await ctrl.put(ev(3))
    assert [q.get_nowait().seq for _ in range(q.qsize())] == [2, 3]


@pytest.mark.asyncio
async def test_drop_newest():
    """策略 C：队列满时丢弃最新。"""
    q = asyncio.Queue(maxsize=2)
    ctrl = BackpressureController("s", q, strategy=BackpressureStrategy.DROP_NEWEST, max_queue_wait=0.05)
    await ctrl.put(ev(1))
    await ctrl.put(ev(2))
    await ctrl.put(ev(3))
    assert [q.get_nowait().seq for _ in range(q.qsize())] == [1, 2]


@pytest.mark.asyncio
async def test_disconnect_strategy():
    """策略 D：队列满时断开慢 Consumer。"""
    q = asyncio.Queue(maxsize=1)
    ctrl = BackpressureController("s", q, strategy=BackpressureStrategy.DISCONNECT, max_queue_wait=0.05)
    await ctrl.put(ev(1))
    with pytest.raises(BackpressureTooSlow):
        await ctrl.put(ev(2))
