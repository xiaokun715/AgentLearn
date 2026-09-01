"""实验 4：Subscriber 永久失败 -> DLQ -> 查看 / Replay / 取消（§20~§21）。"""
from __future__ import annotations

import pytest

from app.domain.delivery import CANCELLED, DLQ, PENDING, SUCCESS
from app.domain.exceptions import NotFoundError

from .conftest import force_due, publish_and_deliver, runtime, seed_subscriber  # noqa: F401


async def _drain_to_dlq(runtime) -> str:
    """让唯一 Delivery 反复失败直到 DLQ，返回 delivery_id。"""
    delivery = (await runtime.repo.list_deliveries())[0]
    for _ in range(4):
        await force_due(runtime, delivery.id)
        await runtime.webhook_worker.process_due()
    return delivery.id


async def test_dlq_list_shows_failed_delivery(runtime, mock):
    mock.set_fail()
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)
    delivery_id = await _drain_to_dlq(runtime)

    entries = await runtime.dlq_service.list_dlq()
    assert len(entries) == 1
    entry = entries[0]
    # §21：DLQ 保存的信息
    assert entry.id == delivery_id
    assert entry.attempt_count == 5
    assert entry.last_error  # 有失败原因
    assert entry.status == DLQ


async def test_dlq_replay_then_success(runtime, mock):
    """§21：Replay -> PENDING -> 重新投递 -> 成功。"""
    mock.set_fail()
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)
    delivery_id = await _drain_to_dlq(runtime)

    # 修复客户（§36 的精神：改好之后重放）
    mock.mode = "ok"
    replayed = await runtime.dlq_service.replay(delivery_id)
    assert replayed.status == PENDING

    delivery = await runtime.repo.get_delivery(delivery_id)
    assert delivery.status == PENDING
    await runtime.webhook_worker.process_due()
    delivery = await runtime.repo.get_delivery(delivery_id)
    assert delivery.status == SUCCESS


async def test_dlq_cancel(runtime, mock):
    mock.set_fail()
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)
    delivery_id = await _drain_to_dlq(runtime)

    cancelled = await runtime.dlq_service.cancel(delivery_id)
    assert cancelled.status == CANCELLED


async def test_replay_non_dlq_rejected(runtime, mock):
    await seed_subscriber(runtime)
    await publish_and_deliver(runtime)
    delivery = (await runtime.repo.list_deliveries())[0]
    with pytest.raises(NotFoundError):
        await runtime.dlq_service.replay(delivery.id)
