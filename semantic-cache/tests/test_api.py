"""HTTP API 测试（设计说明书 §3 / §48）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from semantic_cache.config import CacheConfig
from semantic_cache.factory import build_cache
from semantic_cache.llm.mock import MockLLM
from semantic_cache.main import create_app


@pytest.fixture
def client():
    config = CacheConfig(store_backend="memory", embedding_provider="mock", threshold=0.83)
    cache = build_cache(config)
    app = create_app(config, cache=cache, llm=MockLLM(delay=0))
    with TestClient(app) as c:
        yield c


def _chat_body(text: str, **kw) -> dict:
    body = {
        "user_id": "user-001",
        "model": "qwen",
        "messages": [{"role": "user", "content": text}],
        "tenant_id": "tenant-A",
    }
    body.update(kw)
    return body


def test_chat_miss_then_exact_hit(client):
    body = _chat_body("什么是TCP？")
    r1 = client.post("/v1/chat", json=body)
    assert r1.status_code == 200
    data1 = r1.json()
    assert data1["cache"]["hit"] is False
    assert data1["cache"]["source"] == "miss"
    assert data1["choices"][0]["message"]["content"].startswith("Mock LLM answer for")

    r2 = client.post("/v1/chat", json=body)
    data2 = r2.json()
    assert data2["cache"]["hit"] is True
    assert data2["cache"]["source"] == "exact"
    assert data2["cache"]["similarity"] == 1.0
    assert data2["cache"]["confidence"] == 1.0
    assert data2["choices"][0]["message"]["content"] == data1["choices"][0]["message"]["content"]


def test_chat_semantic_hit_over_http(client):
    client.post("/v1/chat", json=_chat_body("什么是TCP协议？"))
    r = client.post("/v1/chat", json=_chat_body("TCP协议是什么？"))
    data = r.json()
    assert data["cache"]["hit"] is True
    assert data["cache"]["source"] == "semantic"
    assert data["cache"]["similarity"] > 0.85


def test_chat_tenant_isolation_over_http(client):
    client.post("/v1/chat", json=_chat_body("什么是TCP？", tenant_id="tenant-A"))
    r = client.post("/v1/chat", json=_chat_body("什么是TCP？", tenant_id="tenant-B"))
    assert r.json()["cache"]["hit"] is False


def test_metrics_endpoint(client):
    client.post("/v1/chat", json=_chat_body("什么是TCP？"))
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "semantic_cache_requests_total" in r.text
    assert "semantic_cache_hit_rate" in r.text
    assert "semantic_cache_tokens_saved" in r.text


def test_health_and_invalidate(client):
    client.post("/v1/chat", json=_chat_body("什么是TCP？"))
    assert client.get("/health").json()["cache_size"] == 1

    r = client.post("/admin/invalidate", json={"tenant_id": "tenant-A"})
    assert r.json()["deleted"] == 1
    assert client.get("/health").json()["cache_size"] == 0


def test_realtime_not_cached(client):
    body = _chat_body("现在几点？", time_sensitive=True)
    r1 = client.post("/v1/chat", json=body)
    assert r1.json()["cache"]["hit"] is False
    r2 = client.post("/v1/chat", json=body)
    assert r2.json()["cache"]["hit"] is False  # 从未写入缓存
