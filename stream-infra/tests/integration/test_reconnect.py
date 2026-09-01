"""断线重连 / Last-Event-ID 重放测试（设计说明书 §22 / §24 / §49）。"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from streaminfra.core.manager import ConcurrentConsumer
from streaminfra.core.state import StreamStatus
from streaminfra.main import create_app
from tests.helpers import parse_sse


@pytest.mark.asyncio
async def test_concurrent_consumer_rejected():
    """单消费者模型：第二个订阅者应被拒绝，释放后可再次订阅。"""
    app = create_app()
    mgr = app.state.stream_manager
    sid = await mgr.create_stream(prompt="x")

    sub = mgr.subscribe(sid)
    await anext(sub)  # 启动订阅并持有消费者槽位
    try:
        with pytest.raises(ConcurrentConsumer):
            async for _ in mgr.subscribe(sid):
                pass
    finally:
        await sub.aclose()  # 释放槽位

    # 释放后可以再次订阅
    sub2 = mgr.subscribe(sid)
    await anext(sub2)
    await sub2.aclose()


@pytest.mark.asyncio
async def test_manager_reconnect_resume():
    """管理器级：消费 3 个 token 后断开，重连从 last_seq 之后续传。"""
    app = create_app()
    mgr = app.state.stream_manager
    sid = await mgr.create_stream(prompt="reconnect")

    got = []
    sub = mgr.subscribe(sid)
    async for ev in sub:
        got.append(ev)
        if sum(1 for e in got if e.type == "token") >= 3:
            break
    await sub.aclose()  # 显式释放消费者槽位（async-for 的 break 不会自动关闭）
    last_seq = max(e.seq for e in got)

    await mgr.disconnect(sid)
    stream = mgr.get(sid)
    assert stream.status == StreamStatus.CANCELLED

    # 重连续传
    resumed = []
    async for ev in mgr.subscribe(sid, last_seq=last_seq):
        resumed.append(ev)
        if ev.type == "done":
            break

    token_seqs = [e.seq for e in resumed if e.type == "token"]
    assert token_seqs, "重连后应继续产出 token"
    assert token_seqs[0] > last_seq
    assert resumed[-1].type == "done"
    assert resumed[-1].data["reason"] == "completed"

    # 等待 Producer 收尾，状态应为 COMPLETED
    if stream.producer is not None:
        try:
            await asyncio.wait_for(stream.producer, timeout=2.0)
        except BaseException:
            pass
    assert stream.status == StreamStatus.COMPLETED
    assert stream.result is not None
    assert stream.result.status == "completed"
    assert stream.result.content


def test_sse_reconnect_with_last_event_id():
    """HTTP 级：先完整消费，再用 Last-Event-ID 从断点重放。"""
    app = create_app()
    with TestClient(app) as client:
        with client.stream("GET", "/v1/chat/stream", params={"prompt": "x"}) as r:
            stream_id = r.headers["x-stream-id"]
            body = "".join(r.iter_text())

        events = parse_sse(body)
        token_seqs1 = [
            int(e["id"]) for e in events if e["event"] == "token" and e["id"] is not None
        ]
        assert token_seqs1
        resume_at = token_seqs1[len(token_seqs1) // 2]

        # 重连：携带 Last-Event-ID，服务端应重放 seq > resume_at 的事件
        with client.stream(
            "GET",
            "/v1/chat/stream",
            params={"stream_id": stream_id},
            headers={"Last-Event-ID": str(resume_at)},
        ) as r2:
            assert r2.status_code == 200
            body2 = "".join(r2.iter_text())

        events2 = parse_sse(body2)
        token_seqs2 = [
            int(e["id"]) for e in events2 if e["event"] == "token" and e["id"] is not None
        ]
        assert token_seqs2
        assert token_seqs2[0] == resume_at + 1
        assert token_seqs2 == list(range(resume_at + 1, token_seqs1[-1] + 1))
        assert any(e["event"] == "done" for e in events2)
