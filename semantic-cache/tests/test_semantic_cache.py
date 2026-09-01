"""SemanticCache 端到端测试（设计说明书 §32 / §49 ~ §50 / §52 测试矩阵）。"""
from __future__ import annotations

import pytest


async def test_full_flow_miss_set_exact_hit(cache, make_request, make_response):
    req = make_request("什么是TCP？")
    r1 = await cache.get(req)
    assert r1.hit is False
    assert r1.source == "miss"

    assert await cache.set(req, make_response("答案A")) is True

    r2 = await cache.get(req)
    assert r2.hit is True
    assert r2.source == "exact"          # 第二次完全相同 -> 精确命中
    assert r2.similarity == 1.0
    assert r2.response["choices"][0]["message"]["content"] == "答案A"


async def test_semantic_hit_for_synonym(cache, make_request, make_response):
    """同义表达：字符串不同但语义高度相似 -> 语义命中（§2 / 实验一）。"""
    await cache.set(make_request("什么是TCP协议？"), make_response("TCP 答案"))
    r = await cache.get(make_request("TCP协议是什么？"))
    assert r.hit is True
    assert r.source == "semantic"
    assert r.similarity > 0.85
    assert r.response["choices"][0]["message"]["content"] == "TCP 答案"


async def test_related_but_different_is_miss(cache, make_request, make_response):
    """相关但不同：相似度低于阈值 -> MISS（§17 错误命中的主要来源）。"""
    await cache.set(make_request("什么是TCP协议？"), make_response())
    r = await cache.get(make_request("什么是UDP协议？"))
    assert r.hit is False


async def test_different_topic_is_miss(cache, make_request, make_response):
    await cache.set(make_request("什么是TCP协议？"), make_response())
    r = await cache.get(make_request("今天天气怎么样？"))
    assert r.hit is False


async def test_different_model_is_miss(cache, make_request, make_response):
    """不同 model 不能共享缓存（§19）。"""
    await cache.set(make_request("什么是TCP？", model="qwen"), make_response())
    r = await cache.get(make_request("什么是TCP？", model="gpt"))
    assert r.hit is False


async def test_different_system_prompt_is_miss(cache, make_request, make_response):
    """不同 system prompt 不能共享缓存（§8 / §52 测试矩阵 System Prompt 行）。"""
    await cache.set(make_request("什么是TCP？", system="系统A"), make_response())
    r = await cache.get(make_request("什么是TCP？", system="系统B"))
    assert r.hit is False


async def test_different_knowledge_version_is_miss(cache, make_request, make_response):
    """知识库版本不同 -> MISS（§25 / §52 测试矩阵 Version 行）。"""
    await cache.set(make_request("什么是TCP？", knowledge_version="v42"), make_response())
    r = await cache.get(make_request("什么是TCP？", knowledge_version="v43"))
    assert r.hit is False


async def test_tools_not_cached(cache, make_request, make_response, metrics):
    """携带 Tool 参数不缓存（§36 / §52 测试矩阵 Tool 行）。"""
    req = make_request("什么是TCP？", tools=[{"type": "function", "function": {"name": "search"}}])
    assert await cache.set(req, make_response()) is False
    assert metrics.skipped_total == 1
    assert metrics.skipped_reasons.get("has_tools") == 1


async def test_time_sensitive_not_cached(cache, make_request, make_response, metrics):
    """实时问题不缓存（§35 / §52 测试矩阵 Realtime 行）。"""
    req = make_request("现在几点？", time_sensitive=True)
    assert await cache.set(req, make_response()) is False
    assert metrics.skipped_reasons.get("time_sensitive") == 1


async def test_high_temperature_not_cached(cache, make_request, make_response, metrics):
    """temperature 过高不缓存（§36）。"""
    req = make_request("写一首诗", temperature=0.9)
    assert await cache.set(req, make_response()) is False
    assert metrics.skipped_reasons.get("temperature_high") == 1


async def test_confidence_has_safety_margin(cache, make_request, make_response):
    """§40：confidence 比 similarity 低一个安全边际。"""
    await cache.set(make_request("什么是TCP协议？"), make_response())
    r = await cache.get(make_request("TCP协议是什么？"))
    assert r.hit is True
    assert r.confidence == pytest.approx(round(r.similarity - 0.02, 4))
    assert 0.0 <= r.confidence <= 1.0


async def test_metrics_tracked(cache, make_request, make_response, metrics):
    """§41 ~ §44：请求/命中/未命中/Token Saved 都被统计。"""
    await cache.set(make_request("什么是TCP？"), make_response())
    await cache.get(make_request("什么是TCP？"))   # exact hit
    await cache.get(make_request("TCP是什么？"))   # semantic hit
    await cache.get(make_request("UDP是什么？"))   # miss

    assert metrics.requests_total == 3
    assert metrics.hits_total == 2
    assert metrics.exact_hits_total == 1
    assert metrics.semantic_hits_total == 1
    assert metrics.misses_total == 1
    assert metrics.hit_rate() == pytest.approx(2 / 3)
    assert metrics.tokens_saved == 2 * (120 + 80)  # §44：每次命中节省一份 usage


async def test_agent_scope_isolation(cache, make_request, make_response):
    """§38 / §39：Business Identity（agent_type/task_type）不能只靠语义相似判定复用。"""
    await cache.set(
        make_request("分析这个 TCP timeout", agent_type="fault_diagnosis", task_type="fault_diagnosis"),
        make_response("建议重启服务"),
    )
    # 不同 agent 复用 -> 语义再相似也要 MISS
    r = await cache.get(make_request("请分析 TCP timeout 原因", agent_type="test_design", task_type="test_design"))
    assert r.hit is False

    # 同一 agent 类型则可复用
    r2 = await cache.get(make_request("请分析 TCP timeout 原因", agent_type="fault_diagnosis", task_type="fault_diagnosis"))
    assert r2.hit is True


async def test_stats_snapshot(cache, make_request, make_response):
    await cache.set(make_request("什么是TCP？"), make_response())
    stats = await cache.stats()
    assert stats["cache_size"] == 1
    assert stats["sets_total"] == 1
