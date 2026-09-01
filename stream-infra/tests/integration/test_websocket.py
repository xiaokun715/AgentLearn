"""WebSocket 端到端测试（设计说明书 §17）。"""
from fastapi.testclient import TestClient

from streaminfra.main import create_app


def test_websocket_full_flow():
    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/ws") as ws:
            ws.send_json({"type": "start", "prompt": "hi"})
            started = ws.receive_json()
            assert started["type"] == "started"
            stream_id = started["stream_id"]

            received = []
            while True:
                msg = ws.receive_json()
                if msg["type"] == "done":
                    assert msg["reason"] == "completed"
                    break
                if msg["type"] == "token":
                    received.append(msg)

            assert len(received) > 0
            seqs = [m["seq"] for m in received]
            assert seqs == sorted(seqs)
            assert len(set(seqs)) == len(seqs)
            assert stream_id


def test_websocket_cancel_and_resume():
    """先取消，再用 last_seq 重连续传（设计说明书 §49 验收场景）。"""
    app = create_app()
    with TestClient(app) as client:
        stream_id = None
        last_seq = 0

        # 第一段：消费 3 个 token 后取消
        with client.websocket_connect("/v1/ws") as ws:
            ws.send_json({"type": "start", "prompt": "hi"})
            started = ws.receive_json()
            stream_id = started["stream_id"]
            tokens = 0
            while tokens < 3:
                msg = ws.receive_json()
                if msg["type"] == "token":
                    tokens += 1
                    last_seq = msg["seq"]
            ws.send_json({"type": "cancel"})
            cancelled = ws.receive_json()
            assert cancelled["type"] == "done"
            assert cancelled["reason"] == "cancelled"

        # 第二段：重连续传
        with client.websocket_connect("/v1/ws") as ws2:
            ws2.send_json({"type": "start", "stream_id": stream_id, "last_seq": last_seq})
            resumed = ws2.receive_json()
            assert resumed["type"] == "resumed"

            seqs = []
            while True:
                msg = ws2.receive_json()
                if msg["type"] == "done":
                    break
                if msg["type"] == "token":
                    seqs.append(msg["seq"])
            assert seqs and seqs[0] > last_seq
            assert seqs == sorted(seqs)
            assert len(set(seqs)) == len(seqs)
