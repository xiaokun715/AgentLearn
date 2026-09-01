"""Rollback 测试（设计说明书 §18 / §37）：回滚不是删除版本。"""
from __future__ import annotations

import pytest

from app.domain.deployment import DeploymentStatus
from app.domain.exceptions import DeploymentError


async def _deploy_v1_then_v2(seeded, runtime):
    """prod 先 v1 100%，再 canary v2 10%（带 A/B 实验）。"""
    pub = runtime.publisher
    await pub.publish("test_case_agent", "prod", seeded["config_v1"], created_by="a")
    dep = await pub.publish(
        "test_case_agent", "prod", seeded["config_v2"], traffic_percent=10,
        experiment="prompt_v2_test", created_by="a", reason="canary",
    )
    return dep


async def test_rollback_restores_previous_routing(seeded, runtime):
    """发布 v2 后回滚 -> 路由恢复 v1 100%，但 v2 版本仍然存在。"""
    dep = await _deploy_v1_then_v2(seeded, runtime)
    rolled = await runtime.rollback_service.rollback(dep.id, created_by="a", reason="tool error")

    assert rolled.status == DeploymentStatus.RELEASED
    assert {r.version: r.weight for r in rolled.rules} == {seeded["config_v1"]: 100}

    # 版本本身没被删除 —— v2 config 与 prompt 都还在（§18：Rollback 不是删除 v13）
    assert await runtime.config_registry.require_config("test_case_agent", seeded["config_v2"]) is not None
    assert await runtime.prompt_registry.require_version(
        "test_case_agent", seeded["prompt_v2"]
    ) is not None


async def test_rollback_to_explicit_version(seeded, runtime):
    """指定 target_version 直接切回某版本。"""
    dep = await _deploy_v1_then_v2(seeded, runtime)
    rolled = await runtime.rollback_service.rollback(
        dep.id, target_version=seeded["config_v1"], created_by="a"
    )
    assert {r.version: r.weight for r in rolled.rules} == {seeded["config_v1"]: 100}


async def test_rollback_without_history_raises(seeded, runtime):
    """首次部署（无 previous_rules）时回滚报错。"""
    dep = await runtime.publisher.publish(
        "test_case_agent", "prod", seeded["config_v1"], created_by="a"
    )
    with pytest.raises(DeploymentError):
        await runtime.rollback_service.rollback(dep.id, created_by="a")


async def test_rollback_clears_experiment(seeded, runtime):
    """回滚后 A/B 实验标记被清除，路由回到单一版本。"""
    dep = await _deploy_v1_then_v2(seeded, runtime)
    assert dep.experiment  # canary 期间带实验
    rolled = await runtime.rollback_service.rollback(dep.id, created_by="a")
    assert rolled.experiment is None
