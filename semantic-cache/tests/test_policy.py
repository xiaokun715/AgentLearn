"""Threshold / Cacheability Policy 测试（设计说明书 §15 ~ §16 / §35 ~ §36）。"""
from __future__ import annotations

import pytest

from semantic_cache.core.entry import CacheEntry, SearchResult
from semantic_cache.core.policy import CachePolicy, ThresholdPolicy

# ---- ThresholdPolicy ----


def _result(sim: float) -> SearchResult:
    entry = CacheEntry(
        cache_id="id", tenant_id="t", model="m", prompt="p", embedding=[], response={},
        created_at=0, expires_at=10 ** 10,
    )
    return SearchResult(entry=entry, similarity=sim)


def test_threshold_boundary():
    policy = ThresholdPolicy(threshold=0.83)
    assert not policy.should_hit(0.82)
    assert policy.should_hit(0.83)
    assert policy.should_hit(0.95)


def test_select_picks_best_above_threshold():
    policy = ThresholdPolicy(threshold=0.83)
    selected = policy.select([_result(0.80), _result(0.90), _result(0.87)])
    assert selected is not None
    assert selected.similarity == pytest.approx(0.90)


def test_select_returns_none_below_threshold():
    policy = ThresholdPolicy(threshold=0.83)
    assert policy.select([_result(0.80), _result(0.75)]) is None
    assert policy.select([]) is None


# ---- CachePolicy ----


@pytest.fixture
def policy():
    return CachePolicy(max_temperature=0.7)


def test_cacheable_by_default(policy, make_request):
    ok, reason = policy.is_cacheable(make_request("什么是TCP？"))
    assert ok is True
    assert reason == ""


def test_high_temperature_not_cacheable(policy, make_request):
    ok, reason = policy.is_cacheable(make_request("什么是TCP？", temperature=0.9))
    assert ok is False
    assert reason == "temperature_high"


def test_tools_not_cacheable(policy, make_request):
    req = make_request("什么是TCP？", tools=[{"type": "function", "function": {"name": "web_search"}}])
    ok, reason = policy.is_cacheable(req)
    assert ok is False
    assert reason == "has_tools"


def test_time_sensitive_not_cacheable(policy, make_request):
    ok, reason = policy.is_cacheable(make_request("现在几点？", time_sensitive=True))
    assert ok is False
    assert reason == "time_sensitive"


@pytest.mark.parametrize(
    "text",
    ["帮我查询订单状态", "现在天气怎么样", "执行删除数据库", "drop table users"],
)
def test_blocklist_non_cacheable(policy, make_request, text):
    ok, reason = policy.is_cacheable(make_request(text))
    assert ok is False
    assert reason.startswith("blocklist:")


def test_response_not_cacheable_flag(policy, make_request):
    response = {"choices": [], "_meta": {"cacheable": False}}
    ok, reason = policy.is_cacheable(make_request("什么是TCP？"), response=response)
    assert ok is False
    assert reason == "response_not_cacheable"


def test_temperature_edge_allowed(policy, make_request):
    ok, _ = policy.is_cacheable(make_request("什么是TCP？", temperature=0.7))
    assert ok is True
