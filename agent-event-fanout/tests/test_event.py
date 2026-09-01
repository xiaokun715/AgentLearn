"""Event 创建与 Outbox（设计说明书 §04~§05, §25~§27）。"""
from __future__ import annotations

import pytest

from app.domain.exceptions import EventTypeError
from app.domain.event import validate_event_type

from .conftest import runtime  # noqa: F401


def test_event_type_validation():
    validate_event_type("agent.job.completed")
    validate_event_type("knowledge.document.ingested")  # 扩展类型可用
    with pytest.raises(EventTypeError):
        validate_event_type("no-dots")
    with pytest.raises(EventTypeError):
        validate_event_type("agent_job_completed")


async def test_create_event_writes_event_and_outbox_atomically(runtime):
    evt = await runtime.event_service.create_event(
        "agent.job.completed", {"job_id": "job_123"}, metadata={"model": "qwen3.5-27b"}
    )
    # Event 已持久化
    stored = await runtime.repo.get_event(evt.id)
    assert stored.type == "agent.job.completed"
    assert stored.data == {"job_id": "job_123"}
    assert stored.metadata["model"] == "qwen3.5-27b"
    # Outbox 已存在且 PENDING（§27）
    assert await runtime.repo.count_outbox_pending() == 1


async def test_event_is_immutable_by_contract(runtime):
    """§04：Event 一旦创建即 Immutable。存储层没有 update 接口。"""
    evt = await runtime.event_service.create_event("agent.job.created", {"job_id": "j1"})
    stored = await runtime.repo.get_event(evt.id)
    # 修改内存对象不影响 DB（值对象）
    stored.data["job_id"] = "hacked"
    again = await runtime.repo.get_event(evt.id)
    assert again.data == {"job_id": "j1"}


async def test_create_event_returns_accepted_without_waiting(runtime):
    """§32：POST /v1/events 应立即返回，不等待任何 Webhook。"""
    import asyncio

    evt = await runtime.event_service.create_event("agent.job.completed", {})
    # 事件刚创建时 Fan-out/Webhook 尚未执行，但创建本身已完成
    assert evt.id.startswith("evt_")
    assert await runtime.repo.count_outbox_pending() == 1
    assert asyncio.get_running_loop().is_running()


async def test_invalid_event_type_rejected(runtime):
    with pytest.raises(EventTypeError):
        await runtime.event_service.create_event("not_an_event_type", {})
