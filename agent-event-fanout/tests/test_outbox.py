"""实验 7：DB 成功但 Queue publish 失败 -> Outbox Pattern 兜底（§25~§27）。"""
from __future__ import annotations

import pytest

from app.domain.outbox import OUTBOX_PENDING, OUTBOX_PUBLISHED

from .conftest import runtime, seed_subscriber  # noqa: F401


async def test_outbox_worker_publishes_and_marks_published(runtime):
    evt = await runtime.event_service.create_event("agent.job.completed", {"job_id": "j1"})
    assert await runtime.repo.count_outbox_pending() == 1

    published = await runtime.outbox_worker.drain_once()
    assert published == 1
    assert await runtime.repo.count_outbox_pending() == 0
    # event_id 已进入 events 队列
    queued = await runtime.event_queue.pop(timeout=0.1)
    assert queued == evt.id


async def test_outbox_rolls_back_on_publish_failure(runtime, monkeypatch):
    """§25 场景：Event 写库成功，但 Queue publish 失败 —— 事件不能丢。"""

    async def broken_publish(payload: str):
        raise ConnectionError("redis down")

    monkeypatch.setattr(runtime.event_queue, "publish", broken_publish)
    evt = await runtime.event_service.create_event("agent.job.completed", {})

    published = await runtime.outbox_worker.drain_once()
    assert published == 0
    # Outbox 回滚为 PENDING，稍后重试（§27）
    assert await runtime.repo.count_outbox_pending() == 1

    # 恢复 Queue 后重新 drain -> 事件最终发布，不丢失（§26）
    monkeypatch.undo()
    published = await runtime.outbox_worker.drain_once()
    assert published == 1
    assert await runtime.repo.count_outbox_pending() == 0


async def test_event_and_outbox_write_atomically(runtime):
    """§26：Event + Outbox 同事务提交 —— 不存在「Event 有、Outbox 没有」。"""
    evt = await runtime.event_service.create_event("agent.job.completed", {})
    rows = await runtime.repo.db.fetchall(
        "SELECT e.id AS eid, o.event_id AS oid"
        " FROM events e LEFT JOIN outbox_events o ON o.event_id = e.id"
    )
    assert len(rows) == 1
    assert rows[0]["eid"] == evt.id
    assert rows[0]["oid"] == evt.id


async def test_outbox_pending_metric(runtime):
    await runtime.event_service.create_event("agent.job.completed", {})
    await runtime.event_service.create_event("agent.job.failed", {})
    assert await runtime.repo.count_outbox_pending() == 2


async def test_outbox_worker_skips_when_empty(runtime):
    assert await runtime.outbox_worker.drain_once() == 0
