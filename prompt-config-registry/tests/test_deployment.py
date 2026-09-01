"""Deployment / Publisher 测试：发布、环境绑定、灰度（设计说明书 §12~§14 / §17）。"""
from __future__ import annotations

import pytest

from app.domain.deployment import DeploymentStatus
from app.domain.exceptions import DeploymentError, NotFoundError


async def test_publish_first_time_is_released(seeded, runtime):
    """环境首次部署没有对照组 -> 直接 100% RELEASED。"""
    dep = await runtime.publisher.publish(
        "test_case_agent", "prod", seeded["config_v1"], created_by="alice"
    )
    assert dep.status == DeploymentStatus.RELEASED
    assert len(dep.rules) == 1
    assert dep.rules[0].version == seeded["config_v1"]
    assert dep.rules[0].weight == 100


async def test_environment_binding_is_isolated(seeded, runtime):
    """dev / staging / prod 各用各的版本（§12）。"""
    pub = runtime.publisher
    await pub.publish("test_case_agent", "dev", seeded["config_v1"], created_by="a")
    await pub.publish("test_case_agent", "staging", seeded["config_v2"], created_by="a")
    await pub.publish("test_case_agent", "prod", seeded["config_v1"], created_by="a")

    dev = await runtime.repo.get_deployment("test_case_agent", "dev")
    staging = await runtime.repo.get_deployment("test_case_agent", "staging")
    prod = await runtime.repo.get_deployment("test_case_agent", "prod")
    assert dev.primary_version == seeded["config_v1"]
    assert staging.primary_version == seeded["config_v2"]
    assert prod.primary_version == seeded["config_v1"]


async def test_canary_publish_keeps_incumbent(seeded, runtime):
    """已有 v1 100% 后发布 v2 10% -> 路由表 [v1:90, v2:10]，状态 CANARY。"""
    pub = runtime.publisher
    await pub.publish("test_case_agent", "prod", seeded["config_v1"], created_by="a")
    dep = await pub.publish(
        "test_case_agent", "prod", seeded["config_v2"], traffic_percent=10,
        experiment="prompt_v2_test", created_by="a", reason="ab test",
    )
    assert dep.status == DeploymentStatus.CANARY
    assert dep.experiment == "prompt_v2_test"
    weights = {r.version: r.weight for r in dep.rules}
    assert weights == {
        seeded["config_v1"]: 90,
        seeded["config_v2"]: 10,
    }


async def test_publish_requires_existing_config(seeded, runtime):
    """发布不存在的 Config 版本 -> 报错。"""
    with pytest.raises(NotFoundError):
        await runtime.publisher.publish(
            "test_case_agent", "prod", 999, created_by="a"
        )


async def test_rollout_progression(seeded, runtime):
    """10% → 30% → 100%：canary 步进，最终 collapse 为单版本 RELEASED。"""
    pub = runtime.publisher
    await pub.publish("test_case_agent", "prod", seeded["config_v1"], created_by="a")
    dep = await pub.publish(
        "test_case_agent", "prod", seeded["config_v2"], traffic_percent=10, created_by="a"
    )

    dep = await pub.rollout(dep.id, seeded["config_v2"], 30, created_by="a")
    assert dep.status == DeploymentStatus.CANARY
    assert {r.version: r.weight for r in dep.rules} == {
        seeded["config_v1"]: 70, seeded["config_v2"]: 30,
    }

    dep = await pub.rollout(dep.id, seeded["config_v2"], 100, created_by="a")
    assert dep.status == DeploymentStatus.RELEASED
    assert len(dep.rules) == 1
    assert dep.rules[0].version == seeded["config_v2"]


async def test_rollout_released_cannot_shrink(seeded, runtime):
    """全量发布后不允许缩回流量（走 rollback 才对）。"""
    pub = runtime.publisher
    dep = await pub.publish("test_case_agent", "prod", seeded["config_v1"], created_by="a")
    with pytest.raises(DeploymentError):
        await pub.rollout(dep.id, seeded["config_v1"], 50, created_by="a")


async def test_rollout_invalid_traffic(seeded, runtime):
    dep = await runtime.publisher.publish(
        "test_case_agent", "prod", seeded["config_v1"], created_by="a"
    )
    with pytest.raises(DeploymentError):
        await runtime.publisher.rollout(dep.id, seeded["config_v1"], 101, created_by="a")


async def test_rollout_single_version_cannot_split(seeded, runtime):
    """单版本部署无法原地切流量：要 canary 必须先 publish 引入新版本。"""
    dep = await runtime.publisher.publish(
        "test_case_agent", "prod", seeded["config_v1"], created_by="a"
    )
    with pytest.raises(DeploymentError):
        await runtime.publisher.rollout(dep.id, seeded["config_v1"], 30, created_by="a")
