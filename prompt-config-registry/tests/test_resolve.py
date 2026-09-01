"""Resolver 测试（设计说明书 §21~§23 / §31）：解析、A/B 路由、缓存失效。"""
from __future__ import annotations

import json

import pytest

from app.cache.keys import deployment_key, snapshot_key
from app.domain.exceptions import NotFoundError


async def _deploy_v1(seeded, runtime, env="prod"):
    return await runtime.publisher.publish(
        "test_case_agent", env, seeded["config_v1"], created_by="a"
    )


async def test_resolve_returns_snapshot(seeded, runtime):
    """resolve 返回可直接执行的配置快照（含展开的 Prompt 模板）。"""
    await _deploy_v1(seeded, runtime)
    snap = await runtime.resolver.resolve("test_case_agent", "prod", "user_123")

    data = snap.to_dict()
    assert data["agent"] == "test_case_agent"
    assert data["config_version"] == seeded["config_v1"]
    assert data["prompt"]["version"] == seeded["prompt_v1"]
    assert data["prompt"]["template"] == "请简洁回答问题。"
    assert data["model"]["name"] == "qwen3.5-27b"
    assert data["parameters"]["temperature"] == 0.2
    assert data["tools"]["version"] == 3
    assert data["routing"]["variant"] == "single"
    # execution_identity 由方法单独提供（HTTP 路由里才会附加到响应体）
    assert "|config:v1|prompt:test_case_agent:v1|" in snap.execution_identity()


async def test_resolve_without_deployment_raises(seeded, runtime):
    with pytest.raises(NotFoundError):
        await runtime.resolver.resolve("test_case_agent", "prod", "user_123")


async def test_resolve_canary_variant(seeded, runtime):
    """A/B 时不同用户稳定命中不同 Variant（Sticky Assignment）。"""
    pub = runtime.publisher
    await pub.publish("test_case_agent", "prod", seeded["config_v1"], created_by="a")
    await pub.publish(
        "test_case_agent", "prod", seeded["config_v2"], traffic_percent=50,
        experiment="prompt_v2_test", created_by="a",
    )

    seen = set()
    for u in ("user_a", "user_b", "user_c", "user_d", "user_e"):
        s1 = await runtime.resolver.resolve("test_case_agent", "prod", u)
        s2 = await runtime.resolver.resolve("test_case_agent", "prod", u)
        assert s1.routing["variant"] == s2.routing["variant"], "Sticky Assignment 失效"
        assert s1.routing["experiment"] == "prompt_v2_test"
        assert s1.routing["variant"] in ("A", "B")
        seen.add(s1.routing["variant"])
    # 50/50 分流下 5 个用户两种 Variant 都出现（P=1-(2/2^5) 极高）
    assert seen == {"A", "B"}


async def test_resolve_caches_snapshot_by_version(seeded, runtime):
    """版本化 key：config v1 的快照被缓存，即使后来发布 v2 也不会串味。"""
    await _deploy_v1(seeded, runtime)
    await runtime.resolver.resolve("test_case_agent", "prod", "user_123")

    # snapshot 已缓存（版本化 key），且包含展开的 Prompt 模板
    raw = await runtime.cache.get(snapshot_key("test_case_agent", seeded["config_v1"]))
    assert raw is not None
    cached = json.loads(raw)
    assert cached["prompt"]["template"] == "请简洁回答问题。"

    # 发布 v2 后，v1 的快照缓存仍然有效（不可变，无需失效）
    await runtime.publisher.publish(
        "test_case_agent", "prod", seeded["config_v2"], traffic_percent=10, created_by="a"
    )
    raw_again = await runtime.cache.get(snapshot_key("test_case_agent", seeded["config_v1"]))
    assert raw_again == raw


async def test_deployment_cache_invalidated_on_change(seeded, runtime):
    """发布/灰度后 deployment 路由缓存必须失效，否则读到旧流量（§23 最关键的坑）。"""
    pub = runtime.publisher
    await pub.publish("test_case_agent", "prod", seeded["config_v1"], created_by="a")

    # 第一次 resolve 会缓存 deploy:{agent}:{env}
    await runtime.resolver.resolve("test_case_agent", "prod", "user_123")
    assert await runtime.cache.get(deployment_key("test_case_agent", "prod")) is not None

    # 发布 v2 10% —— 触发 cache invalidation
    dep = await pub.publish(
        "test_case_agent", "prod", seeded["config_v2"], traffic_percent=10, created_by="a"
    )
    assert await runtime.cache.get(deployment_key("test_case_agent", "prod")) is None

    # 再解析应读到新路由
    snap = await runtime.resolver.resolve("test_case_agent", "prod", "user_123")
    assert await runtime.cache.get(deployment_key("test_case_agent", "prod")) is not None
    assert "rules" in snap.routing and len(snap.routing["rules"]) == 2


async def test_execution_identity_changes_with_prompt_version(seeded, runtime):
    """Prompt 版本变了，执行身份就变 —— Semantic Cache Key 必须据此区分（§33）。"""
    await runtime.publisher.publish("test_case_agent", "prod", seeded["config_v1"], created_by="a")
    id1 = (await runtime.resolver.resolve("test_case_agent", "prod", "u1")).execution_identity()
    await runtime.publisher.publish("test_case_agent", "prod", seeded["config_v2"], created_by="a")
    id2 = (await runtime.resolver.resolve("test_case_agent", "prod", "u1")).execution_identity()
    assert id1 != id2
    assert "prompt:test_case_agent:v1" in id1
    assert "prompt:test_case_agent:v2" in id2
