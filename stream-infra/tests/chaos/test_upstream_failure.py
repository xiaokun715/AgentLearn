"""上游部分失败（设计说明书 §26 / §27 / §43 / §44）。

Streaming 开始后不能返回 HTTP 500：错误必须变成 Stream Event。
"""
from fastapi.testclient import TestClient

from streaminfra.config import StreamConfig
from streaminfra.main import create_app
from streaminfra.provider.mock_llm import MockLLMProvider
from tests.helpers import parse_sse


def test_upstream_failure_partial():
    config = StreamConfig(provider_delay=0.01)
    app = create_app(
        config=config,
        provider_factory=lambda prompt, stream_id: MockLLMProvider(
            tokens=["a", "b", "c", "d", "e"],
            delay=0.01,
            fail_after=3,          # 第 4 个 token 处抛错
            fail_code="UPSTREAM_TIMEOUT",
        ),
    )
    with TestClient(app) as client:
        with client.stream("GET", "/v1/chat/stream", params={"prompt": "x"}) as r:
            stream_id = r.headers["x-stream-id"]
            assert r.status_code == 200  # 绝不返回 500
            body = "".join(r.iter_text())

        events = parse_sse(body)
        # [DONE] 结束符没有 event 字段，需过滤掉
        types = [e["event"] for e in events if e["event"] is not None]
        assert types.count("token") >= 3
        assert "error" in types
        assert types[-1] == "done"

        # 部分结果（§27）：已生成的 token 不丢
        res = client.get(f"/v1/streams/{stream_id}/result").json()
        assert res["status"] == "failed"
        assert res["content"] == "abc"
        assert res["error"]["code"] == "UPSTREAM_TIMEOUT"
        assert res["error"]["retryable"] is True

    # 指标：失败计数 +1
    metrics = app.state.metrics
    assert metrics.stream_failed_total >= 1
