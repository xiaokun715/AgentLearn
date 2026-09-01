"""Replay Buffer 单测（设计说明书 §23 / §24 / §25）。"""
import pytest

from streaminfra.buffer.base import ResumeWindowExpired
from streaminfra.buffer.memory import MemoryReplayBuffer
from streaminfra.core.event import StreamEvent


def ev(seq: int) -> StreamEvent:
    return StreamEvent(stream_id="s", seq=seq, type="token", data={"delta": str(seq)})


@pytest.mark.asyncio
async def test_append_and_replay():
    buf = MemoryReplayBuffer(max_events=100)
    for i in range(1, 7):
        await buf.append(ev(i))
    assert await buf.oldest_seq() == 1
    assert await buf.last_seq() == 6
    assert [e.seq for e in await buf.replay(0)] == [1, 2, 3, 4, 5, 6]
    assert [e.seq for e in await buf.replay(3)] == [4, 5, 6]
    assert [e.seq for e in await buf.replay(6)] == []


@pytest.mark.asyncio
async def test_maxlen_eviction_window():
    buf = MemoryReplayBuffer(max_events=3)
    for i in range(1, 8):
        await buf.append(ev(i))
    assert await buf.oldest_seq() == 5
    assert await buf.last_seq() == 7
    assert [e.seq for e in await buf.replay(0)] == [5, 6, 7]
    # 客户端声称已收到 seq=4，但 4 已被淘汰 -> Replay Window 过期（§25）
    with pytest.raises(ResumeWindowExpired):
        await buf.validate_replay_window("s", 4)


@pytest.mark.asyncio
async def test_validate_window_edges():
    buf = MemoryReplayBuffer(max_events=10)
    # last_seq=0 表示从头开始，永远合法
    await buf.validate_replay_window("s", 0)
    # 空缓冲下 last_seq>0 视为"客户端超前于服务端" -> 非法
    with pytest.raises(ResumeWindowExpired):
        await buf.validate_replay_window("s", 5)
    for i in range(1, 4):
        await buf.append(ev(i))
    await buf.validate_replay_window("s", 0)
    await buf.validate_replay_window("s", 3)
    # 4 超前于 newest=3 -> 非法（防止非法游标导致连接挂起）
    with pytest.raises(ResumeWindowExpired):
        await buf.validate_replay_window("s", 4)
