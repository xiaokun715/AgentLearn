"""Cosine Similarity 计算测试（设计说明书 §14）。

顺带标注 Mock Embedding 在同义/相关/无关三类样本上的分布，
作为 Threshold 校准的参考（§16 实验二）。
"""
from __future__ import annotations

import pytest

from semantic_cache.embedding.mock import MockEmbeddingGenerator
from semantic_cache.search.base import cosine_similarity, l2_normalize

# ---- 纯数学正确性 ----


def test_cosine_identical_is_one():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_is_minus_one():
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_zero_vector_is_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_length_mismatch_is_zero():
    assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0


def test_l2_normalize_unit_norm():
    v = l2_normalize([3.0, 4.0])
    assert sum(x * x for x in v) == pytest.approx(1.0)


# ---- Mock 向量在同义/相关/无关上的分布（§16 标定依据）----


@pytest.fixture
def emb():
    return MockEmbeddingGenerator()


async def test_mock_separates_synonym_from_related(emb):
    """同义对必须显著高于「相关但不同」对，否则单一阈值无法区分（§17）。"""
    syn = cosine_similarity(await emb.embed("什么是TCP协议？"), await emb.embed("TCP协议是什么？"))
    related = cosine_similarity(await emb.embed("什么是TCP协议？"), await emb.embed("什么是UDP协议？"))
    assert syn > 0.85
    assert related < 0.80
    assert syn - related > 0.08


async def test_mock_related_below_unrelated_gap(emb):
    related = cosine_similarity(await emb.embed("什么是TCP？"), await emb.embed("UDP是什么？"))
    unrelated = cosine_similarity(await emb.embed("什么是TCP？"), await emb.embed("今天天气怎么样？"))
    assert related > unrelated
    assert unrelated < 0.50
