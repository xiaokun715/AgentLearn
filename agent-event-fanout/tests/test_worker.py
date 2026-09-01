"""WebhookWorker —— 成功 / 重试 / 超时 / DLQ 路径（§34, §10, §37）。"""
from __future__ import annotations

from app.domain.delivery import DLQ, RETRYING, SUCCESS

from .conftest import (
    force_due,
    next_retry_delta,
    publish_and_deliver,
    runtime,
    seed_subscriber,
)  # noqa: F401


async def _first_delivery(runtime):
    deliveries = await runtime.repo.list_deliveries()
    assert deliveries, "尚无 Delivery"
    return deliveries[0]


async def test_success_path(runtime, mock):
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)
    delivery = await _first_delivery(runtime)
    assert delivery.status == SUCCESS
    assert delivery.attempt_count == 1
    assert delivery.response_status == 200
    assert delivery.last_error is None
    assert len(mock.requests) == 1


async def test_retry_then_success(runtime, mock):
    """§36：随机失败客户最终成功 —— 前 2 次 503，第 3 次 200。"""
    mock.set_fail_then_ok(fail_count=2)
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)  # attempt 1 -> 503 -> RETRYING
    delivery = await _first_delivery(runtime)
    assert delivery.status == RETRYING
    assert delivery.attempt_count == 1
    assert "503" in delivery.last_error

    await force_due(runtime, delivery.id)
    await runtime.webhook_worker.process_due()  # attempt 2 -> 503 -> RETRYING
    delivery = await runtime.repo.get_delivery(delivery.id)
    assert delivery.status == RETRYING
    assert delivery.attempt_count == 2

    await force_due(runtime, delivery.id)
    await runtime.webhook_worker.process_due()  # attempt 3 -> 200 -> SUCCESS
    delivery = await runtime.repo.get_delivery(delivery.id)
    assert delivery.status == SUCCESS
    assert delivery.attempt_count == 3
    assert len(mock.received_for(delivery.id)) == 3  # 三次 HTTP


async def test_timeout_is_a_failure_and_retries(runtime, mock):
    """§37：Webhook Timeout 本质上也是一种失败 -> 重试。"""
    mock.set_slow(seconds=2.0)  # 慢于 request_timeout=1.0
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)
    delivery = await _first_delivery(runtime)
    assert delivery.status == RETRYING
    assert "timeout" in delivery.last_error.lower()


async def test_dlq_after_max_attempts(runtime, mock):
    """§20：一直 503，超过 max_attempts 进 DLQ。"""
    mock.set_fail()
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)  # attempt 1
    delivery = await _first_delivery(runtime)
    for _ in range(4):  # attempt 2..5
        await force_due(runtime, delivery.id)
        await runtime.webhook_worker.process_due()
    delivery = await runtime.repo.get_delivery(delivery.id)
    assert delivery.status == DLQ
    assert delivery.attempt_count == 5
    assert "max_attempts" in delivery.last_error

    dlq_list = await runtime.dlq_service.list_dlq()
    assert [d.id for d in dlq_list] == [delivery.id]


async def test_permanent_failure_goes_straight_to_dlq(runtime, mock):
    """§18：400 等永久错误不重试，直接 DLQ。"""
    mock.set_fail(status=400)
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)
    delivery = await _first_delivery(runtime)
    assert delivery.status == DLQ
    assert delivery.attempt_count == 1
    assert "不可重试" in delivery.last_error


async def test_429_retry_after_respected(runtime, mock):
    """实验 3 / §38：429 + Retry-After 时，下次重试时间 = now + Retry-After。"""
    mock.set_ratelimit(retry_after=30)
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)
    delivery = await _first_delivery(runtime)
    assert delivery.status == RETRYING
    assert delivery.attempt_count == 1
    # 尊重服务端 backpressure，而不是指数退避（base_delay=0 时退避会是 0s）
    assert next_retry_delta(delivery) == 30.0


async def test_metrics_counters_update(runtime, mock):
    """§40：成功/重试/DLQ 计数。"""
    # 单个订阅者：第一个事件 -> 成功
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)
    assert runtime.metrics.delivery_success == 1

    # 客户开始故障：第二个事件 -> 5 次失败 -> DLQ
    mock.set_fail()
    evt = await runtime.event_service.create_event("agent.job.completed", {})
    await runtime.outbox_worker.drain_once()
    await runtime.fanout_consumer.drain_once()
    delivery = (await runtime.repo.list_deliveries())[0]
    await runtime.webhook_worker.process_due()  # attempt 1 -> retry
    for _ in range(4):
        await force_due(runtime, delivery.id)
        await runtime.webhook_worker.process_due()  # attempts 2..5

    assert runtime.metrics.delivery_success == 1
    assert runtime.metrics.delivery_retry == 4   # 前 4 次进入重试
    assert runtime.metrics.delivery_dlq == 1
    assert runtime.metrics.attempts_total == 6   # 1 成功 + 5 失败
    text = runtime.metrics.render()
    assert "webhook_delivery_success_total" in text
    assert "webhook_dlq_total 1" in text
