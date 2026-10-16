"""共享 fixtures。

每个测试都拿到**全新**的平台 —— 数据源可用性、网关审计、假设状态都会变，
共享实例会让「单独跑能过、一起跑就挂」的测试变得无法定位。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:      # 允许直接 pytest，不必先 pip install -e .
    sys.path.insert(0, str(ROOT))

from app.domain.models import Event, ToolCall  # noqa: E402
from app.factory import PlatformConfig, RiskPlatform, build_platform  # noqa: E402

#: 演示用的三个案件：``(世界, 实体, 裁判结论)``
CASES = [
    ("group_fraud", "u_fraud_00", "CONFIRMED_RISK"),
    ("false_positive", "u_normal_00", "LIKELY_FALSE_POSITIVE"),
    ("sparse_data", "u_sparse_00", "INSUFFICIENT_EVIDENCE"),
]


@pytest.fixture
def make_platform():
    def _make(world: str = "group_fraud", **kwargs) -> RiskPlatform:
        return build_platform(world, PlatformConfig(tool_config=None, **kwargs))

    return _make


@pytest.fixture
def platform(make_platform) -> RiskPlatform:
    return make_platform()


@pytest.fixture
def alert(platform):
    def _make(entity: str = "u_fraud_00", **kwargs):
        return platform.make_alert(entity, "规则与模型分歧，转人工调查",
                                   model_score=0.32, **kwargs)

    return _make


@pytest.fixture
def event_factory():
    def _make(user_id: str, device_id: str, campaign_id: str = "cmp_001") -> Event:
        return Event(event_type="claim_coupon", user_id=user_id,
                     device_id=device_id, campaign_id=campaign_id)

    return _make


__all__ = ["CASES", "ToolCall", "Event"]
