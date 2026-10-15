"""共享 fixtures。

两个约定：

1. **每个测试独立装配**（``build_platform``），避免经验库 / 证据库互相污染；
2. 需要观察时间行为的测试注入 ``ManualClock``，让「超时」「缓存过期」不用真等。
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:      # 允许直接 `pytest` 而不必先 pip install -e .
    sys.path.insert(0, str(ROOT))

from app.clock import ManualClock  # noqa: E402
from app.domain.enums import Observation  # noqa: E402
from app.domain.models import Budget  # noqa: E402
from app.factory import Platform, PlatformConfig, build_platform  # noqa: E402
from app.sim import BASE_TIME  # noqa: E402


@pytest.fixture
def make_platform():
    """工厂 fixture：``make_platform("env")`` 拿一个干净实例。"""
    created: list[Platform] = []

    def _make(world: str = "env", *, clock=None, **config_overrides) -> Platform:
        config = PlatformConfig.for_demo()
        for key, value in config_overrides.items():
            setattr(config, key, value)
        platform = build_platform(config, world=world, clock=clock)
        created.append(platform)
        return platform

    yield _make


@pytest.fixture
def env_platform(make_platform) -> Platform:
    return make_platform("env")


@pytest.fixture
def manual_clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def tiny_budget() -> Budget:
    return Budget(
        max_steps=2, max_tool_calls=6, max_seconds=600.0, max_recovery_attempts=3
    )


def cached_check(observation: Observation, *, version: str = "V100R020C10",
                 age_minutes: float = 1.0) -> dict:
    """构造一条 ``cached_checks`` 条目（供模式 B 的测试使用）。"""
    return {
        "observation": observation.value,
        "software_version": version,
        "collected_at": (BASE_TIME - timedelta(minutes=age_minutes)).isoformat(),
    }


@pytest.fixture
def cache_entry():
    return cached_check
