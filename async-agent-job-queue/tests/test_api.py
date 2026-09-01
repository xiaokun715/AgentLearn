"""HTTP API（设计说明书 §8-10 / §28 / §36）。"""
from __future__ import annotations

import asyncio

from .conftest import wait_terminal


async def test_create_job_returns_immediately(api):
    resp = await api.post(
        "/v1/jobs",
        json={"agent": "research_agent", "input": {"query": "NVIDIA"}},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["job_id"].startswith("job_")
    assert body["status"] == "queued"  # 立即返回，不等待执行


async def test_query_job_until_success(api, runtime):
    resp = await api.post(
        "/v1/jobs",
        json={"agent": "research_agent", "input": {"query": "NVIDIA"}},
    )
    job_id = resp.json()["job_id"]
    # 轮询直到 success
    for _ in range(300):
        r = await api.get(f"/v1/jobs/{job_id}")
        data = r.json()
        if data["status"] == "success":
            break
        await asyncio.sleep(0.02)
    assert data["status"] == "success"
    assert data["result"]["report"]
    assert data["progress"] == 100


async def test_create_job_unknown_agent_404(api):
    resp = await api.post("/v1/jobs", json={"agent": "no_such_agent"})
    assert resp.status_code == 404


async def test_get_missing_job_404(api):
    resp = await api.get("/v1/jobs/job_nope")
    assert resp.status_code == 404


async def test_cancel_endpoint(api, runtime):
    resp = await api.post(
        "/v1/jobs",
        json={"agent": "research_agent", "input": {"query": "q"}},
    )
    job_id = resp.json()["job_id"]
    cr = await api.post(f"/v1/jobs/{job_id}/cancel")
    assert cr.status_code == 200
    assert cr.json()["status"] == "cancelled"


async def test_events_endpoint(api, runtime):
    resp = await api.post(
        "/v1/jobs",
        json={"agent": "research_agent", "input": {"query": "q"}},
    )
    job_id = resp.json()["job_id"]
    for _ in range(300):
        r = await api.get(f"/v1/jobs/{job_id}")
        if r.json()["status"] == "success":
            break
        await asyncio.sleep(0.02)
    ev = await api.get(f"/v1/jobs/{job_id}/events")
    types = [e["type"] for e in ev.json()]
    assert "JOB_CREATED" in types
    assert "JOB_COMPLETED" in types
    assert "CHECKPOINT_SAVED" in types
    assert "TOOL_CALLED" in types


async def test_metrics_endpoint(api, runtime):
    await api.post("/v1/jobs", json={"agent": "research_agent", "input": {"query": "q"}})
    resp = await api.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert "agent_jobs_created_total" in text
    assert "agent_queue_depth" in text
    assert "agent_worker_active" in text


async def test_dlq_flow_via_api(api, runtime):
    # 永久失败 -> DEAD -> 通过 API 从 DLQ 重投
    resp = await api.post(
        "/v1/jobs",
        json={
            "agent": "chaos_agent",
            "input": {
                "query": "q",
                "chaos": {"fail_at": ["search"], "fail_attempts": {"search": 999},
                          "fail_with": "retryable"},
            },
        },
    )
    job_id = resp.json()["job_id"]
    for _ in range(500):
        r = await api.get(f"/v1/jobs/{job_id}")
        if r.json()["status"] == "dead":
            break
        await asyncio.sleep(0.02)
    assert r.json()["status"] == "dead"

    dlq = await api.get("/v1/dlq")
    assert any(e["job_id"] == job_id for e in dlq.json())

    retry = await api.post(f"/v1/dlq/{job_id}/retry")
    assert retry.status_code == 200
    assert retry.json()["status"] == "queued"
    q = await api.get(f"/v1/jobs/{job_id}")
    assert q.json()["status"] == "queued"


async def test_healthz(api):
    r = await api.get("/healthz")
    assert r.json() == {"status": "ok"}
