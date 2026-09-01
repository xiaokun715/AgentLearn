"""租户隔离测试（设计说明书 §20 / §52 测试矩阵 Tenant 行）。

这是必须实现的安全边界：
  - 缓存条目必须带 tenant_id；
  - 查询/检索时必须 WHERE tenant_id = current_tenant。
否则 Tenant A 的内部数据会被 Tenant B 语义命中，造成跨租户数据泄露。
"""
from __future__ import annotations

from semantic_cache.core.entry import CacheEntry


async def test_exact_not_shared_across_tenants(cache, make_request, make_response):
    await cache.set(make_request("公司2025年营收是多少？", tenant="tenant-A"), make_response("A 的内部数据"))
    r = await cache.get(make_request("公司2025年营收是多少？", tenant="tenant-B"))
    assert r.hit is False


async def test_semantic_not_shared_across_tenants(cache, make_request, make_response):
    """语义相似也必须在 store 层被租户过滤拦截（§20 / §31）。"""
    await cache.set(make_request("什么是TCP？", tenant="tenant-A"), make_response("A 的答案"))
    r = await cache.get(make_request("TCP是什么？", tenant="tenant-B"))  # 同义但换租户
    assert r.hit is False
    assert r.source == "miss"


async def test_same_tenant_still_hits(cache, make_request, make_response):
    await cache.set(make_request("什么是TCP？", tenant="tenant-A"), make_response("A 的答案"))
    r = await cache.get(make_request("什么是TCP？", tenant="tenant-A"))
    assert r.hit is True


async def test_safety_validator_double_checks_tenant(cache, make_request):
    """即使 store 没过滤干净，Safety Validator 也要兜底（§18）。"""
    req = make_request("什么是TCP？", tenant="tenant-B")
    entry = CacheEntry(
        cache_id="x", namespace="semantic-cache", tenant_id="tenant-A", model="qwen",
        prompt="什么是tcp?", embedding=[], response={},
        created_at=0, expires_at=10 ** 10, fingerprint="f", system_fingerprint=None,
    )
    ok, reasons = cache.validator.validate(req, entry, system_fingerprint=None)
    assert ok is False
    assert "tenant" in reasons
