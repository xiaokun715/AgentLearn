"""实验 5 / 6：Idempotency 与 At-least-once（§09, §22~§24, §46）。

- 实验 5：同一个 Delivery 被重复消费，只有一次真正执行。
- 实验 6：HTTP 请求成功但响应丢失 -> Sender 重试 -> 客户收到两次
  （At-least-once）；但 X-Webhook-ID 相同，Receiver 可幂等去重。
"""
from __future__ import annotations

from .conftest import publish_and_deliver, runtime, seed_subscriber  # noqa: F401


async def _enqueue_delivery(runtime):
    """创建事件并走到 Fan-out（Delivery 处于 PENDING、已入队），返回 delivery。"""
    evt = await runtime.event_service.create_event("agent.job.completed", {"job_id": "j1"})
    await runtime.outbox_worker.drain_once()
    await runtime.fanout_consumer.drain_once()
    return (await runtime.repo.list_deliveries())[0]


async def test_same_delivery_consumed_twice_only_sends_once(runtime, mock):
    """实验 5：同一 Delivery 被消费两次，第二次是 no-op。"""
    await seed_subscriber(runtime)
    delivery = await _enqueue_delivery(runtime)
    assert delivery.status == "PENDING"

    # 第一次消费：真正执行
    first = await runtime.webhook_worker.deliver_delivery(delivery.id)
    assert first is not None
    assert first.status == "SUCCESS"
    # 第二次消费：原子领取失败 -> None（§09 幂等）
    second = await runtime.webhook_worker.deliver_delivery(delivery.id)
    assert second is None

    assert len(mock.received_for(delivery.id)) == 1  # HTTP 只发了一次


async def test_concurrent_delivery_claim_is_atomic(runtime, mock):
    """并发领取同一 Delivery：只有一个 worker 能真正执行。"""
    await seed_subscriber(runtime)
    delivery = await _enqueue_delivery(runtime)

    import asyncio

    results = await asyncio.gather(
        *[runtime.webhook_worker.deliver_delivery(delivery.id) for _ in range(5)]
    )
    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1  # 只有一个拿到了 claim
    assert len(mock.received_for(delivery.id)) == 1


async def test_at_least_once_duplicate_on_response_loss(runtime, mock):
    """实验 6：服务端已执行但响应丢失 -> Sender 重试 -> 收到两次（幂等键相同）。"""
    mock.set_drop()
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)
    delivery = (await runtime.repo.list_deliveries())[0]

    # 响应丢失 -> 发送方视为失败 -> 重试 -> 第二次成功
    assert delivery.status != "SUCCESS"

    from .conftest import force_due

    mock.mode = "ok"
    await force_due(runtime, delivery.id)
    await runtime.webhook_worker.process_due()
    delivery = await runtime.repo.get_delivery(delivery.id)
    assert delivery.status == "SUCCESS"

    # 客户实际收到两次（At-least-once 的客观事实）
    received = mock.received_for(delivery.id)
    assert len(received) == 2
    # 但幂等键 X-Webhook-ID 相同 —— Receiver 据此去重
    ids = {r["headers"].get("X-Webhook-ID") for r in received}
    assert ids == {delivery.id}


async def test_delivery_id_distinguishes_per_subscriber(runtime, mock):
    """§24：X-Webhook-ID 是 Delivery ID，一个 Event 的不同 Subscriber 不同。"""
    from .conftest import seed_subscriber as _seed

    await _seed(runtime, url="http://mock/crm")
    await _seed(runtime, url="http://mock/ticket")
    await publish_and_deliver(runtime)
    webhook_ids = {r["headers"].get("X-Webhook-ID") for r in mock.requests}
    assert len(webhook_ids) == 2
