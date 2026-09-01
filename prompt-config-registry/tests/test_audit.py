"""审计测试（设计说明书 §24~§25）：每一次变更都留下可归因的记录。"""
from __future__ import annotations


async def test_every_mutation_is_audited(seeded, runtime):
    """创建 prompt / version / config / 部署 / 灰度 / 回滚 全部落审计。"""
    entries = await runtime.audit_service.list()
    actions = [e.action for e in entries]
    assert "CREATE_PROMPT" in actions
    assert "CREATE_PROMPT_VERSION" in actions
    assert "CREATE_CONFIG" in actions

    pub = runtime.publisher
    await pub.publish("test_case_agent", "prod", seeded["config_v1"], created_by="alice")
    dep = await pub.publish(
        "test_case_agent", "prod", seeded["config_v2"], traffic_percent=10,
        created_by="alice", reason="canary",
    )
    await pub.rollout(dep.id, seeded["config_v2"], 100, created_by="alice", reason="promote")
    await runtime.rollback_service.rollback(dep.id, created_by="ops", reason="tool error")

    entries = await runtime.audit_service.list()
    assert [e.action for e in entries].count("DEPLOY") >= 2
    assert [e.action for e in entries].count("ROLLOUT") == 1
    assert [e.action for e in entries].count("ROLLBACK") == 1


async def test_audit_has_before_after_and_reason(seeded, runtime):
    """发布审计带 before/after/reason，能回答"到底变了什么"。"""
    await runtime.publisher.publish("test_case_agent", "prod", seeded["config_v1"], created_by="a")
    dep = await runtime.publisher.publish(
        "test_case_agent", "prod", seeded["config_v2"], traffic_percent=10,
        created_by="alice", reason="Improve tool selection",
    )
    entry = next(e for e in await runtime.audit_service.list() if e.action == "DEPLOY" and e.reason)
    assert entry.reason == "Improve tool selection"
    assert entry.after["rules"][0]["version"] == seeded["config_v1"]
    assert entry.before["status"] == "RELEASED"
    assert entry.actor == "alice"
    assert entry.resource_id == "test_case_agent:prod"


async def test_audit_filter_by_action_and_agent(seeded, runtime):
    await runtime.publisher.publish("test_case_agent", "prod", seeded["config_v1"], created_by="a")

    deploys = await runtime.audit_service.list(action="DEPLOY")
    assert deploys and all(e.action == "DEPLOY" for e in deploys)

    for_agent = await runtime.audit_service.list(agent="test_case_agent")
    assert for_agent and all("test_case_agent" in e.resource_id for e in for_agent)
