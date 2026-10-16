"""共享 fixtures。

每个测试都拿到**全新**的 Platform —— 池子、模板库、路由都互不共享。
这一点不是洁癖：池子是可变的（Agent 会下线、成本会被覆盖），
共享一个实例会让「单独跑能过、一起跑就挂」的测试变得无法定位。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:      # 允许直接 pytest，不必先 pip install -e .
    sys.path.insert(0, str(ROOT))

from app.domain.enums import ExecutionMode, TemplateFate  # noqa: E402
from app.domain.models import RunRequest  # noqa: E402
from app.factory import Platform, PlatformConfig, build_platform  # noqa: E402

TROUBLE_QUERY = "SRS 测试失败了，帮我定位一下根因"
REVIEW_QUERY = "帮我看看这段代码有没有安全问题"
TREND_QUERY = "最近吞吐趋势怎么样，画个图"


@pytest.fixture
def make_platform():
    created: list[Platform] = []

    def _make(**overrides) -> Platform:
        config = PlatformConfig(agent_config=None, **overrides)
        platform = build_platform(config)
        created.append(platform)
        return platform

    yield _make


@pytest.fixture
def platform(make_platform) -> Platform:
    return make_platform()


@pytest.fixture
def request_factory():
    def _make(query: str, **kwargs) -> RunRequest:
        return RunRequest(query=query, **kwargs)

    return _make


__all__ = [
    "TROUBLE_QUERY", "REVIEW_QUERY", "TREND_QUERY",
    "ExecutionMode", "TemplateFate", "RunRequest",
]
