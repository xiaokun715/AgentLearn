"""Canary —— 灰度发布控制器（设计说明书 §17）。

A/B 是「v12 vs v13 同流量对比」；Canary 是「把新版本从 5% 逐步放到 100%」：
    5% → 10% → 30% → 50% → 100%

每一步都监控 Error Rate / Latency / Token Cost / Tool Call Error / Success Rate；
一旦异常就 Rollback（§18 / §37）。
"""
from __future__ import annotations

from ..domain.deployment import Deployment, DeploymentStatus

# 推荐的灰度步进（§17）
CANARY_STEPS: tuple[int, ...] = (5, 10, 30, 50, 100)


def next_traffic_step(current: int) -> int | None:
    """从当前流量取下一个灰度档位；已经是 100% 时返回 None。"""
    for step in CANARY_STEPS:
        if step > current:
            return step
    return None


def next_status(percent: int) -> str:
    """流量决定状态：<100% 是 CANARY，=100% 是 RELEASED。"""
    return DeploymentStatus.CANARY if percent < 100 else DeploymentStatus.RELEASED


def allowed_rollout(dep: Deployment, percent: int) -> bool:
    """校验一次 rollout 是否合法：
    - 目标流量必须在 0~100；
    - 已是 RELEASED（100%）后不允许再改；
    - CANARY 状态允许调整（前进或后退都行，方便实时纠偏）。
    """
    if not 0 <= percent <= 100:
        return False
    if dep.status == DeploymentStatus.RELEASED and percent < 100:
        # 已全量发布，不允许再缩回（请走新 publish / rollback）
        return False
    if dep.is_single_version and percent < 100 and dep.traffic_percent == 100:
        # 单版本 100% 无法原地切流量 —— 需要先 publish 新版本
        return False
    return True


def canary_report(dep: Deployment) -> dict:
    """生成一张灰度进度报告（供 /v1/deployments/{id} 展示）。"""
    steps = [
        {"target_percent": s, "reached": dep.traffic_percent >= s}
        for s in CANARY_STEPS
    ]
    return {
        "agent": dep.agent_name,
        "environment": dep.environment,
        "status": dep.status,
        "current_percent": dep.traffic_percent,
        "next_step": next_traffic_step(dep.traffic_percent),
        "steps": steps,
        "primary_version": dep.primary_version,
    }
