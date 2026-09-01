"""实验 1：一个 Event -> 三个 Subscriber，验证 Fan-out（§08, §33）。"""
from __future__ import annotations

import pytest

from app.domain.exceptions import NotFoundError

from .conftest import publish_and_deliver, runtime, seed_subscriber  # noqa: F401


async def _seed_three(runtime, *, events=None):
    evs = events or ["agent.job.completed"]
    subs = []
    for name in ("crm", "ticket", "slack"):
        subs.append(
            await seed_subscriber(
                runtime, url=f"http://mock/{name}", events=evs, secret="whsec_test"
            )
        )
    return subs


async def test_one_event_fans_out_to_three_subscribers(runtime, mock):
    subs = await _seed_three(runtime)
    event_id = await publish_and_deliver(runtime)

    # 每个 Subscriber 各收到 1 次（Fan-out 1 -> 3）
    assert len(mock.requests) == 3
    delivery_ids = {r["headers"].get("X-Webhook-ID") for r in mock.requests}
    assert len(delivery_ids) == 3

    # 三条 Delivery 都 SUCCESS
    deliveries = await runtime.repo.list_deliveries()
    assert len(deliveries) == 3
    assert {d.subscriber_id for d in deliveries} == {s.id for s in subs}
    assert all(d.status == "SUCCESS" for d in deliveries)


async def test_fanout_only_to_matching_subscribers(runtime, mock):
    """§07：只订阅了该事件类型的 Subscriber 才收到。"""
    await _seed_three(runtime, events=["agent.job.completed"])
    # 一个只订阅 failed 事件的 Subscriber 不应收到 completed 事件
    await seed_subscriber(
        runtime, url="http://mock/other", events=["agent.job.failed"]
    )
    await publish_and_deliver(runtime, type_="agent.job.completed")

    assert len(mock.requests) == 3  # other 未收到
    paths = {r["path"] for r in mock.requests}
    assert paths == {"/crm", "/ticket", "/slack"}


async def test_disabled_subscriber_is_skipped(runtime, mock):
    await seed_subscriber(runtime, url="http://mock/crm")
    await seed_subscriber(runtime, url="http://mock/disabled", status="disabled")
    await publish_and_deliver(runtime)
    assert len(mock.requests) == 1  # 只发给 active 的 crm


async def test_delivery_body_contains_event_fields(runtime, mock):
    await seed_subscriber(runtime)
    event_id = await publish_and_deliver(runtime, data={"job_id": "job_999"})
    import json

    body = json.loads(mock.requests[0]["body"])
    assert body["id"] == event_id
    assert body["type"] == "agent.job.completed"
    assert body["data"] == {"job_id": "job_999"}


async def test_fanout_unknown_event_raises(runtime):
    with pytest.raises(NotFoundError):
        await runtime.fanout_service.fanout_event("evt_missing")


async def test_delivery_uniqueness_after_double_fanout(runtime, mock):
    """§09：UNIQUE(event_id, subscriber_id) —— 重复 Fan-out 不会重复建 Delivery。"""
    sub = await seed_subscriber(runtime)
    event_id = await publish_and_deliver(runtime)
    # 再次触发 Fan-out（例如队列重复消息）
    await runtime.fanout_service.fanout_event(event_id)
    deliveries = await runtime.repo.list_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].subscriber_id == sub.id
    assert len(mock.requests) == 1  # 只投递一次
