"""SSE 端到端测试（设计说明书 §3.1 / §14 / §22 / §25）。"""
from fastapi.testclient import TestClient

from streaminfra.config import StreamConfig
from streaminfra.main import create_app
from tests.helpers import parse_sse


def test_sse_full_flow():
    app = create_app()
    with TestClient(app) as client:
        with client.stream("GET", "/v1/chat/stream", params={"prompt": "测试"}) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            stream_id = resp.headers["x-stream-id"]
            body = "".join(resp.iter_text())

        events = parse_sse(body)
        # [DONE] 结束符没有 event 字段，需过滤掉
        types = [e["event"] for e in events if not e["comment"] and e["event"] is not None]
        assert "metadata" in types
        assert any(t == "token" for t in types)
        assert types[-1] == "done"

        # seq 严格递增且无重复（心跳为 comment，id 为空，不参与断言）
        ids = [int(e["id"]) for e in events if e["id"] is not None]
        assert ids == sorted(ids)
        assert len(ids) == len(set(ids))

        # OpenAI 风格结束符
        assert body.rstrip().endswith("data: [DONE]")

        # 最终结果可查询（§27）
        res = client.get(f"/v1/streams/{stream_id}/result")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "completed"
        assert data["usage"]["output_tokens"] >= 1
        assert data["content"]
        assert data["ttft"] >= 0
        assert data["total_latency"] >= 0


def test_sse_404_unknown_stream():
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/chat/stream", params={"stream_id": "no-such-stream"})
        assert resp.status_code == 404
        assert resp.json()["error"] == "stream_not_found"


def test_sse_409_resume_window_expired():
    """Replay Window 过期：客户端 last_seq 已被淘汰 -> 409（§25）。"""
    app = create_app(config=StreamConfig(max_events=3, provider_delay=0.001))
    with TestClient(app) as client:
        with client.stream("GET", "/v1/chat/stream", params={"prompt": "x"}) as resp:
            stream_id = resp.headers["x-stream-id"]
            body = "".join(resp.iter_text())
        assert "data: [DONE]" in body
        # 完整消费后 Replay Buffer 只保留最后 3 条，last_seq=5 早已过期
        resp = client.get(
            "/v1/chat/stream",
            params={"stream_id": stream_id, "last_seq": 5},
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["error"] == "resume_window_expired"
        assert data["reason"] == "behind"
        assert data["oldest_seq"] > 5


def test_sse_409_last_seq_ahead_of_server():
    """客户端声称已收到超过服务端产出的 seq（非法游标）-> 409，绝不能挂起。"""
    app = create_app(config=StreamConfig(provider_delay=0.001))
    with TestClient(app) as client:
        with client.stream("GET", "/v1/chat/stream", params={"prompt": "x"}) as resp:
            stream_id = resp.headers["x-stream-id"]
            body = "".join(resp.iter_text())
        assert "data: [DONE]" in body
        # last_seq 远超服务端已产出的最大 seq
        resp = client.get(
            "/v1/chat/stream",
            params={"stream_id": stream_id, "last_seq": 10 ** 18},
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["error"] == "resume_window_expired"
        assert data["reason"] == "ahead"
        assert data["newest_seq"] < 10 ** 18


def test_sse_rejects_concurrent_consumer(live_server):
    """单消费者模型：已有活跃订阅者时，第二个连接返回 409 concurrent_consumer。

    注：ASGITransport 会缓冲完整响应，中途连接无法被观测，故用真实服务器验证。
    """
    import httpx

    app = create_app(config=StreamConfig(provider_delay=0.05))
    base = live_server(app)
    with httpx.Client(base_url=base, timeout=15) as client:
        with client.stream("GET", "/v1/chat/stream", params={"prompt": "x"}) as r1:
            stream_id = r1.headers["x-stream-id"]
            # 读取首行，确认第一个订阅者已持有消费者槽位
            for line in r1.iter_lines():
                if line.startswith("id: "):
                    break
            resp = client.get("/v1/chat/stream", params={"stream_id": stream_id})
            assert resp.status_code == 409
            assert resp.json()["error"] == "concurrent_consumer"
