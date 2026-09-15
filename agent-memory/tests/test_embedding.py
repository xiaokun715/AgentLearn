"""嵌入：确定性 / 相似度单调性 / 空向量与兜底。"""
from __future__ import annotations

import math

import pytest

from memory_engine.embedding import KeywordEmbedder, cosine_similarity, l2_normalize


@pytest.fixture()
def emb() -> KeywordEmbedder:
    return KeywordEmbedder(dim=128)


def test_identical_text_is_1(emb):
    v1, v2 = emb.embed("排查 5G 基站闪断"), emb.embed("排查 5G 基站闪断")
    assert cosine_similarity(v1, v2) == pytest.approx(1.0, abs=1e-6)


def test_deterministic_across_instances(emb):
    other = KeywordEmbedder(dim=128)
    assert emb.embed("NR 切换失败怎么定位") == other.embed("NR 切换失败怎么定位")


def test_related_beats_unrelated(emb):
    q = emb.embed("排查 5G 基站闪断")
    related = cosine_similarity(q, emb.embed("5G 基站闪断 退服计数 切换失败率"))
    unrelated = cosine_similarity(q, emb.embed("今天天气很好适合出去散步"))
    assert related > unrelated
    assert unrelated < 0.5


def test_dimension_and_unit_norm(emb):
    v = emb.embed("VoLTE 掉话要先补 4G 邻区")
    assert len(v) == 128
    norm = math.sqrt(sum(x * x for x in v))
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_empty_text_yields_zero_vector(emb):
    assert emb.embed("") == [0.0] * 128
    assert emb.embed("   ") == [0.0] * 128


def test_cosine_zero_vector_is_safe():
    assert cosine_similarity([], [1.0, 2.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0  # 维度不一致


def test_l2_normalize_preserves_zero():
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_embed_many_lengths(emb):
    vecs = emb.embed_many(["a 故障", "b 闪断", "c 干扰"])
    assert [len(v) for v in vecs] == [128, 128, 128]
