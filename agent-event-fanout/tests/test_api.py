"""API 端点测试（设计说明书 §31~§32, §39, §40）。"""
from __future__ import annotations

import httpx
import pytest

from .conftest import publish_and_deliver, runtime, seed_subscriber  # noqa: F401


@pytest.fixture
async def client(runtime):
    from app.main import create_app

    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---- Subscriber API ------------------------------------------------------
async def test_create_subscriber_returns_secret_once(client):
    resp = await client.post(
        "/v1/subscribers",
        json={"url": "http://localhost:9001/webhook", "events": ["agent.job.completed"]},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"].startswith("sub_")
    assert body["secret"].startswith("whsec_")  # §31：secret 仅返回一次


async def test_create_subscriber_validates_events(client):
    resp = await client.post(
        "/v1/subscribers",
        json={"url": "http://x/", "events": ["not-a-type"]},
    )
    assert resp.status_code == 422  # Pydantic 校验


async def test_list_subscribers(client):
    await client.post("/v1/subscribers", json={
        "url": "http://x/crm", "events": ["agent.job.completed"]})
    resp = await client.get("/v1/subscribers")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_patch_subscriber_status_disables(client, runtime):
    created = await client.post("/v1/subscribers", json={
        "url": "http://x/crm", "events": ["agent.job.completed"]})
    sub_id = created.json()["id"]
    resp = await client.patch(f"/v1/subscribers/{sub_id}", json={"status": "disabled"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    # disabled 的订阅者不会被 Fan-out 匹配
    matched = await runtime.repo.match_subscribers("agent.job.completed")
    assert matched == []


# ---- Event API -----------------------------------------------------------
async def test_create_event_returns_accepted(client):
    resp = await client.post("/v1/events", json={
        "type": "agent.job.completed",
        "data": {"job_id": "job_123", "result": {"answer": "hello"}},
    })
    assert resp.status_code == 202  # §32：立即返回，不等待 Webhook
    body = resp.json()
    assert body["event_id"].startswith("evt_")
    assert body["status"] == "accepted"


async def test_create_event_invalid_type_400(client):
    resp = await client.post("/v1/events", json={"type": "bad_type"})
    assert resp.status_code == 400


async def test_get_event(client):
    created = await client.post("/v1/events", json={"type": "agent.job.completed", "data": {}})
    event_id = created.json()["event_id"]
    resp = await client.get(f"/v1/events/{event_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == event_id


async def test_get_missing_event_404(client):
    resp = await client.get("/v1/events/evt_none")
    assert resp.status_code == 404


# ---- Delivery API --------------------------------------------------------
async def test_delivery_status_query(client, runtime):
    """§39：GET /v1/deliveries/{id} 返回状态 / 尝试次数 / 错误。"""
    await seed_subscriber(runtime, url="http://x/crm")
    event_id = await publish_and_deliver(runtime)
    deliveries = await runtime.repo.list_deliveries()
    delivery_id = deliveries[0].id

    resp = await client.get(f"/v1/deliveries/{delivery_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["event_id"] == event_id
    assert body["status"] == "SUCCESS"
    assert body["attempt_count"] == 1


async def test_list_deliveries_filter_by_status(client, runtime, mock):
    mock.set_fail()
    await seed_subscriber(runtime, url="http://x/bad")
    await publish_and_deliver(runtime)
    resp = await client.get("/v1/deliveries?status=RETRYING")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_dlq_replay_endpoint(client, runtime, mock):
    mock.set_fail()
    await seed_subscriber(runtime, url="http://x/bad")
    await publish_and_deliver(runtime)
    delivery = (await runtime.repo.list_deliveries())[0]
    from .conftest import force_due

    for _ in range(4):
        await force_due(runtime, delivery.id)
        await runtime.webhook_worker.process_due()
    assert delivery.status == "DLQ" or (await runtime.repo.get_delivery(delivery.id)).status == "DLQ"

    resp = await client.post(f"/v1/deliveries/{delivery.id}/replay")
    assert resp.status_code == 200
    assert resp.json()["status"] == "PENDING"


async def test_metrics_endpoint(client, runtime):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "webhook_events_total" in resp.text
    assert "webhook_delivery_latency_p95_ms" in resp.text


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
