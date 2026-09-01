"""Mock Embedding Generator（设计说明书 §11 / §51）。

第一版不要依赖真实 Embedding 模型。这里用「字符 n-gram 特征哈希 + L2 归一化」
生成确定性的伪语义向量（feature hashing embedding）：

  - 共享越多 n-gram → 余弦相似度越高（同义表达 ≈ 0.85+）
  - 主题不同 → 共享 n-gram 少 → 余弦相似度低（≈ 0.7 以下）

它足以驱动缓存全链路的验证（MISS -> LLM -> 写入 -> 相似命中），
但数值是示意性的 —— 接入真实模型后必须按 §16 用正负样本对重新标定阈值，
不能直接沿用本项目为 Mock 校准的阈值。
"""
from __future__ import annotations

import hashlib

import numpy as np

from .base import EmbeddingGenerator

# 权重：unigram 承载主题，bigram/trigram 承载局部搭配。unigram 主导可让「语序调整」的
# 同义句（如「什么是TCP」/「TCP是什么」）仍获得较高相似度。
_NGRAM_WEIGHTS: dict[int, float] = {1: 1.0, 2: 0.5, 3: 0.25}


def _ngrams(text: str, n: int) -> list[str]:
    if len(text) < n:
        return [text] if text else []
    return [text[i : i + n] for i in range(len(text) - n + 1)]


def _hash_index(gram: str, dim: int) -> int:
    return int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16) % dim


class MockEmbeddingGenerator(EmbeddingGenerator):
    def __init__(self, dim: int = 1024, max_ngram: int = 3):
        self.dim = dim
        self.max_ngram = max_ngram
        self._cache: dict[str, list[float]] = {}

    async def embed(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            return list(cached)

        vec = np.zeros(self.dim, dtype=np.float32)
        for n in range(1, self.max_ngram + 1):
            weight = _NGRAM_WEIGHTS.get(n, 0.0)
            for gram in _ngrams(text, n):
                vec[_hash_index(gram, self.dim)] += weight

        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm

        result = vec.tolist()
        self._cache[text] = result
        return list(result)
