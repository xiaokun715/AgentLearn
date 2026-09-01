"""Canary 控制器测试（设计说明书 §17）。"""
from __future__ import annotations

from app.domain.deployment import Deployment, DeploymentRule, DeploymentStatus
from app.router.canary import (
    allowed_rollout,
    canary_report,
    next_status,
    next_traffic_step,
)


def _dep(rules, status=DeploymentStatus.CANARY) -> Deployment:
    return Deployment(id="d", agent_name="a", environment="prod", status=status, rules=rules)


def test_next_step_progression():
    """5% → 10% → 30% → 50% → 100%，之后返回 None。"""
    assert next_traffic_step(0) == 5
    assert next_traffic_step(5) == 10
    assert next_traffic_step(30) == 50
    assert next_traffic_step(50) == 100
    assert next_traffic_step(100) is None


def test_status_by_traffic():
    assert next_status(10) == DeploymentStatus.CANARY
    assert next_status(100) == DeploymentStatus.RELEASED


def test_allowed_rollout_guards():
    two = [DeploymentRule(version=12, weight=90), DeploymentRule(version=13, weight=10)]
    single = [DeploymentRule(version=13, weight=100)]

    # CANARY 双版本：允许调流量
    assert allowed_rollout(_dep(two), 30)
    assert allowed_rollout(_dep(two), 5)
    # 非法流量
    assert not allowed_rollout(_dep(two), -1)
    assert not allowed_rollout(_dep(two), 101)
    # 已 RELEASED 不允许缩回
    assert not allowed_rollout(_dep(single, DeploymentStatus.RELEASED), 50)
    # 单版本 100% 无法原地切流量
    assert not allowed_rollout(_dep(single), 50)


def test_canary_report():
    dep = _dep([DeploymentRule(version=12, weight=90), DeploymentRule(version=13, weight=10)])
    report = canary_report(dep)
    assert report["status"] == DeploymentStatus.CANARY
    assert report["current_percent"] == 90  # primary_version 的流量
    assert report["next_step"] == 100
    steps = {s["target_percent"]: s["reached"] for s in report["steps"]}
    assert steps[5] is True and steps[10] is True and steps[100] is False
