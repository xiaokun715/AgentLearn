"""代码审查发现的缺陷回归测试。

- CRITICAL：重复 Fanout 命中 UNIQUE 约束后连接残留隐式事务，导致后续
  ``claim_*`` 抛 OperationalError 且锁泄漏。
- HIGH：Delivery 卡在 DELIVERING / Outbox 卡在 PROCESSING（Worker 崩溃），
  超过领取租约后必须能被回收重投。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.storage.models import to_db

from .conftest import publish_and_deliver, runtime, seed_subscriber  # noqa: F401


async def test_duplicate_fanout_does_not_break_connection(runtime, mock):
    """CRITICAL 回归：ConflictError 后连接不得残留隐式事务，后续 claim 必须正常。"""
    await seed_subscriber(runtime)
    evt = await runtime.event_service.create_event("agent.job.completed", {"job_id": "j1"})
    await runtime.outbox_worker.drain_once()
    await runtime.fanout_consumer.drain_once()

    # 第二次 Fan-out：create_delivery 全部命中 UNIQUE(event_id, subscriber_id)
    # -> ConflictError（修复前会残留未提交的隐式事务）
    created = await runtime.fanout_service.fanout_event(evt.id)
    assert created == 0

    # 关键：之后 claim（BEGIN 事务）必须还能工作 —— 否则说明连接被污染
    await runtime.webhook_worker.process_due()
    delivery = (await runtime.repo.list_deliveries())[0]
    assert delivery.status == "SUCCESS"

    # 再跑一个完整事件，验证系统整体仍健康（无锁泄漏 / 死锁）
    await publish_and_deliver(runtime, data={"job_id": "j2"})
    ds = await runtime.repo.list_deliveries()
    assert all(d.status == "SUCCESS" for d in ds)


async def test_conflict_error_within_transaction_rolls_back(runtime, mock):
    """CRITICAL：INSERT 违反约束时，execute() 内必须回滚，不能污染事务上下文。"""
    await seed_subscriber(runtime)
    evt = await runtime.event_service.create_event("agent.job.completed", {})
    await runtime.outbox_worker.drain_once()
    await runtime.fanout_consumer.drain_once()
    await runtime.fanout_service.fanout_event(evt.id)  # ConflictError 路径
    # 事务上下文内的操作也要正常（例如再建订阅者走 transaction()）
    sub2 = await seed_subscriber(runtime, url="http://mock/ticket")
    assert sub2.id
    # 连接的 in_transaction 应为 False（无残留隐式事务）
    assert runtime.repo.db._conn.in_transaction is False


async def test_stale_delivering_reclaimed_by_lease(runtime, mock):
    """HIGH 回归：卡在 DELIVERING 超过租约的 Delivery 会被回收重投（at-least-once 兜底）。"""
    await seed_subscriber(runtime)
    await runtime.event_service.create_event("agent.job.completed", {})
    await runtime.outbox_worker.drain_once()
    await runtime.fanout_consumer.drain_once()
    delivery = (await runtime.repo.list_deliveries())[0]

    # 模拟 Worker 崩溃：Delivery 已领取过一次（attempt=1）卡在 DELIVERING，
    # updated_at 早在租约之前
    delivery.status = "DELIVERING"
    delivery.attempt_count = 1
    delivery.updated_at = delivery.updated_at - timedelta(seconds=1000)
    await runtime.repo.update_delivery(delivery)

    # 下一次扫描应回收（租约 120s < 1000s）并重新投递
    n = await runtime.webhook_worker.process_due()
    assert n == 1
    delivery = await runtime.repo.get_delivery(delivery.id)
    assert delivery.status == "SUCCESS"
    assert delivery.attempt_count == 2


async def test_fresh_delivering_not_reclaimed(runtime, mock):
    """租约内（updated_at 很新）的 DELIVERING 不应被回收，避免重复投递。"""
    await seed_subscriber(runtime)
    await runtime.event_service.create_event("agent.job.completed", {})
    await runtime.outbox_worker.drain_once()
    await runtime.fanout_consumer.drain_once()
    delivery = (await runtime.repo.list_deliveries())[0]

    delivery.status = "DELIVERING"
    delivery.updated_at = datetime.now(timezone.utc)  # 刚领取，租约未过期
    await runtime.repo.update_delivery(delivery)

    n = await runtime.webhook_worker.process_due()
    assert n == 0  # 未被回收
    delivery = await runtime.repo.get_delivery(delivery.id)
    assert delivery.status == "DELIVERING"


async def test_stale_outbox_processing_reclaimed(runtime):
    """HIGH 回归：卡在 PROCESSING 超过租约的 Outbox 条目会被回收重新发布。"""
    await runtime.event_service.create_event("agent.job.completed", {})

    # 第一次 claim -> 变 PROCESSING（模拟崩溃后未 mark PUBLISHED）
    entries = await runtime.repo.claim_outbox_entries(10)
    assert len(entries) == 1

    # 把条目 updated_at 改到租约之前
    await runtime.repo.db.execute(
        "UPDATE outbox_events SET updated_at=? WHERE id=?",
        to_db(datetime.now(timezone.utc) - timedelta(seconds=1000)), entries[0].id,
    )

    # 再次 claim 应回收该 PROCESSING 条目
    reclaimed = await runtime.repo.claim_outbox_entries(10)
    assert len(reclaimed) == 1
    assert reclaimed[0].id == entries[0].id
