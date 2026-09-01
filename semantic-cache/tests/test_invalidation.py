"""Invalidation 测试（设计说明书 §24 ~ §25）。"""
from __future__ import annotations


async def test_invalidate_by_tenant(cache, make_request, make_response):
    await cache.set(make_request("什么是TCP？", tenant="A"), make_response())
    await cache.set(make_request("什么是TCP？", tenant="B"), make_response())
    assert await cache.store.count() == 2

    deleted = await cache.invalidate(tenant_id="A")
    assert deleted == 1
    assert await cache.store.count() == 1


async def test_invalidate_by_model(cache, make_request, make_response):
    await cache.set(make_request("什么是TCP？", model="qwen"), make_response())
    await cache.set(make_request("什么是TCP？", model="gpt"), make_response())
    assert await cache.invalidate(model="qwen") == 1
    assert await cache.store.count() == 1


async def test_invalidate_single_cache_id(cache, make_request, make_response):
    await cache.set(make_request("什么是TCP？"), make_response())
    (entry,) = list(cache.store._entries.values())
    assert await cache.invalidate(cache_id=entry.cache_id) == 1
    assert await cache.store.count() == 0


async def test_version_based_invalidation_keeps_other_versions(cache, make_request, make_response):
    """§25：v42 -> v43 时只失效旧版本条目，不碰其他条目。"""
    await cache.set(make_request("什么是TCP？", knowledge_version="v42"), make_response())
    await cache.set(make_request("什么是UDP？", knowledge_version="v43"), make_response())

    deleted = await cache.invalidation.invalidate_by_version("v42")
    assert deleted == 1
    assert await cache.store.count() == 1

    # 剩下的 v43 条目仍可精确命中
    r = await cache.get(make_request("什么是UDP？", knowledge_version="v43"))
    assert r.hit is True


async def test_version_mismatch_blocks_hit(cache, make_request, make_response):
    """§25：当前知识库 v43，缓存里是 v42 的答案 -> 必须 MISS。"""
    await cache.set(make_request("什么是TCP？", knowledge_version="v42"), make_response())
    r = await cache.get(make_request("什么是TCP？", knowledge_version="v43"))
    assert r.hit is False
