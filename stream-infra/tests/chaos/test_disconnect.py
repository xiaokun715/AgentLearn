"""断线处理（设计说明书 §19 / §20）：Client 断开 -> 取消上游 LLM。

说明：ASGITransport 不会在客户端中途关闭连接时通知应用断开，
因此 HTTP 级断线场景用真实 uvicorn 服务器验证（见 conftest.live_server）。
"""
import asyncio
import time

import httpx
import pytest

from streaminfra.config import StreamConfig
from streaminfra.core.state import StreamStatus
from streaminfra.main import create_app
from tests.helpers import parse_metric


@pytest.mark.asyncio
async def test_manager_disconnect_cancels_upstream():
    """管理器级：订阅一个 token 后断线，Producer 应被取消、状态为 CANCELLED。"""
    app = create_app()
    mgr = app.state.stream_manager
    sid = await mgr.create_stream(prompt="x")

    sub = mgr.subscribe(sid)
    async for ev in sub:
        if ev.type == "token":
            break
    await sub.aclose()  # 显式释放消费者槽位

    await mgr.disconnect(sid)
    stream = mgr.get(sid)
    if stream.producer is not None:
        try:
            await asyncio.wait_for(stream.producer, timeout=2.0)
        except BaseException:
            pass
    assert stream.status == StreamStatus.CANCELLED
    assert app.state.metrics.stream_disconnect_total >= 1
    # 取消只应被 finalize 一次（cancel 幂等），cancelled_total 精确为 1
    assert app.state.metrics.stream_cancelled_total == 1
    # 部分结果应被记录（断线时的 partial usage，§20）
    assert stream.result is not None
    assert stream.result.status == "cancelled"


def test_http_disconnect_cancels_upstream(live_server):
    """HTTP 级：读到一个 token 后关闭连接，服务端应取消上游 LLM。"""
    app = create_app(config=StreamConfig(provider_delay=0.02, poll_interval=0.05))
    base = live_server(app)

    with httpx.Client(base_url=base, timeout=10) as client:
        with client.stream("GET", "/v1/chat/stream", params={"prompt": "x"}) as r:
            stream_id = r.headers["x-stream-id"]
            for line in r.iter_lines():
                if line.startswith("event: token"):
                    break  # 立即关闭连接

        # 服务端应检测到断开并取消上游，最终结果状态为 cancelled
        status = None
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                status = client.get(f"/v1/streams/{stream_id}/result").json().get("status")
            except Exception:
                pass
            if status in ("cancelled", "completed", "failed"):
                break
            time.sleep(0.05)
        assert status == "cancelled", f"实际状态: {status}"

        metrics = client.get("/metrics").text
        assert parse_metric(metrics, "stream_disconnect_total") >= 1
