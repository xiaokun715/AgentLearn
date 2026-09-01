"""Mock Embedding 测试（设计说明书 §10 ~ §11）。"""
from __future__ import annotations

import math

import pytest

from semantic_cache.embedding.mock import MockEmbeddingGenerator
from semantic_cache.search.base import cosine_similarity


@pytest.fixture
def emb() -> MockEmbeddingGenerator:
    return MockEmbeddingGenerator()


async def test_dimension(emb):
    vec = await emb.embed("什么是TCP？")
    assert len(vec) == 1024


async def test_l2_normalized(emb):
    vec = await emb.embed("什么是TCP？")
    norm = math.sqrt(sum(v * v for v in vec))
    assert norm == pytest.approx(1.0, abs=1e-4)


async def test_deterministic(emb):
    a = await emb.embed("什么是TCP？")
    b = await emb.embed("什么是TCP？")
    assert a == b


async def test_similar_texts_closer_than_different(emb):
    va = await emb.embed("什么是TCP协议？")
    vb = await emb.embed("TCP协议是什么？")      # 同义
    vc = await emb.embed("今天天气怎么样？")     # 完全不同
    assert cosine_similarity(va, vb) > 0.85
    assert cosine_similarity(va, vc) < 0.50


async def test_cached_reuses_object(emb):
    a = await emb.embed("什么是TCP？")
    b = await emb.embed("什么是TCP？")
    assert b is not a  # 返回拷贝，防止外部篡改内部缓存
