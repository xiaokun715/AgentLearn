"""HTTP API + SSE 实时流测试（交互层）。"""
from __future__ import annotations

import asyncio


async def _wait_http_terminal(client, task_id: str, terminal=("completed", "cancelled", "failed")):
    for _ in range(400):
        r = await client.get(f"/v1/tasks/{task_id}")
        body = r.json()
        if body["status"] in terminal:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} 未在预期时间内进入终态")


async def test_submit_returns_202_with_task_id(api):
    r = await api.post("/v1/tasks", json={"query": "查账单", "days": 1, "agent": "research_agent"})
    assert r.status_code == 202
    body = r.json()
    assert body["task_id"].startswith("task_")
    assert body["status"] == "running"
    assert body["events_url"].endswith("/events")


async def test_cancel_via_http(api):
    r = await api.post("/v1/tasks", json={"query": "查一下并取消", "days": 1})
    task_id = r.json()["task_id"]

    r = await api.post(f"/v1/tasks/{task_id}/cancel", json={"mode": "force"})
    assert r.status_code == 200
    assert r.json()["cancel_requested"] is True

    final = await _wait_http_terminal(api, task_id)
    assert final["status"] == "cancelled"

    hist = (await api.get(f"/v1/tasks/{task_id}/history")).json()
    types = [e["type"] for e in hist]
    assert "CANCELLATION_REQUESTED" in types
    assert "TASK_CANCELLED" in types


async def test_unknown_task_returns_404(api):
    r = await api.get("/v1/tasks/task_does_not_exist")
    assert r.status_code == 404


async def test_invalid_agent_returns_400(api):
    r = await api.post("/v1/tasks", json={"query": "x", "agent": "no_such_agent"})
    assert r.status_code == 400


# ---- SSE：实时广播一条链路直到终态 ----
async def test_sse_live_stream_to_completion(api):
    r = await api.post("/v1/tasks", json={"query": "跑完整并实时推送", "days": 1})
    task_id = r.json()["task_id"]

    seen = []
    async with api.stream("GET", f"/v1/tasks/{task_id}/events") as resp:
        assert "text/event-stream" in resp.headers["content-type"]
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                ev = line[len("event: "):]
                seen.append(ev)
                if ev == "TASK_COMPLETED":
                    break
    assert "TASK_STARTED" in seen
    assert "STEP_STARTED" in seen
    assert "TASK_COMPLETED" in seen


# ---- SSE：晚到客户端也能通过“历史重放”看到已结束任务的终态 ----
async def test_sse_replays_history_after_terminal(runtime, api):
    t = runtime.service.submit(query="立刻取消", days=1)
    runtime.service.cancel(t.task_id, mode="force")   # pre-start -> 同步进入终态

    seen = []
    async with api.stream("GET", f"/v1/tasks/{t.task_id}/events") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                seen.append(line[len("event: "):])
                if line[len("event: "):] == "TASK_CANCELLED":
                    break
    assert "CANCELLATION_REQUESTED" in seen
    assert "TASK_CANCELLED" in seen
