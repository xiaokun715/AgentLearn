"""TTL 测试（设计说明书 §22 ~ §23 / §52 测试矩阵 Expired 行）。"""
from __future__ import annotations

import asyncio
import time

import pytest


async def test_expired_entry_is_miss(cache, store, metrics, make_request, make_response):
    req = make_request("什么是TCP？")
    assert await cache.set(req, make_response(), ttl=3600) is True

    # 命中一次
    r = await cache.get(req)
    assert r.hit is True

    # 人为把过期时间拨到过去（等价于 TTL 到期）
    (entry,) = list(store._entries.values())
    entry.expires_at = time.time() - 1

    r2 = await cache.get(req)
    assert r2.hit is False
    assert r2.source == "miss"
    assert metrics.expiration_total == 1


async def test_real_ttl_short_lived(cache, make_request, make_response):
    req = make_request("什么是TCP？")
    assert await cache.set(req, make_response(), ttl=0.05) is True

    assert (await cache.get(req)).hit is True

    await asyncio.sleep(0.15)
    assert (await cache.get(req)).hit is False


async def test_ttl_expires_at_is_default(cache, make_request, make_response):
    req = make_request("什么是TCP？")
    await cache.set(req, make_response())  # 使用默认 TTL
    (entry,) = list(cache.store._entries.values())
    assert entry.expires_at - entry.created_at == pytest.approx(3600, abs=0.1)
