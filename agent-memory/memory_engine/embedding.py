"""向量数学与“伪嵌入”。

说明书的记忆检索是**向量语义混合搜索**，但 Demo 不接外部 Embedding 模型，也不引 numpy。
这里提供两块：

1. ``cosine_similarity`` / ``l2_normalize`` —— 照说明书纯 ``math`` 移植；
2. ``KeywordEmbedder`` —— 一个确定性的“伪嵌入器”：把文本切成字符 1~3-gram，
   用 ``zlib.crc32`` 落进固定维度的词袋桶，再 L2 归一。
   它在“语义相似=词面重叠”这一层足够好用：相似句式分数高、无关句子分数低，
   且同一文本每次向量完全一致(不依赖进程随机的 ``hash()``)。

真实产品里把它换成 OpenAI/Qwen/DashScope 的 embedding 即可 —— 引擎只看 ``list[float]``。
"""
from __future__ import annotations

import math
import re
import zlib

_WORD_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """两个向量的余弦相似度(说明书§混合检索同款)。

    空/零向量或维度不齐一律返回 0.0(宁可漏召回也别除零崩溃)。
    """
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm_a = math.sqrt(sum(a * a for a in v1))
    norm_b = math.sqrt(sum(b * b for b in v2))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def l2_normalize(v: list[float]) -> list[float]:
    """L2 归一化；全零向量原样返回。"""
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0.0:
        return v
    return [x / norm for x in v]


class KeywordEmbedder:
    """字符 n-gram 词袋伪嵌入器(确定性，跨进程稳定)。

    >>> e = KeywordEmbedder(dim=64)
    >>> len(e.embed("上海今天有暴雨")) == 64
    True
    """

    def __init__(self, dim: int = 256, n_gram_range: tuple[int, int] = (1, 3)) -> None:
        self.dim = dim
        self.n_gram_range = n_gram_range

    # ------------------------------------------------------------------
    def _ngrams(self, text: str) -> list[str]:
        """分词后对每个 token 切 1~3-gram 字符片，中文整句按字切。"""
        grams: list[str] = []
        lo, hi = self.n_gram_range
        for token in _WORD_RE.findall(text.lower()):
            if len(token) == 1:
                grams.append(token)
                continue
            # 对 4+ 字长词/整句按滑动窗口切 n-gram，捕捉“短语级”特征
            for n in range(lo, hi + 1):
                grams.extend(token[i : i + n] for i in range(len(token) - n + 1))
        return grams

    @staticmethod
    def _bucket(gram: str, dim: int) -> int:
        """crc32 稳定哈希 → 桶下标(0~dim-1)。不依赖进程随机 seed。"""
        return zlib.crc32(gram.encode("utf-8")) % dim

    def embed(self, text: str) -> list[float]:
        """text → L2 归一化词袋向量。空文本返回全零(cosine 会兜底为 0)。"""
        if not text or not text.strip():
            return [0.0] * self.dim
        bag = [0.0] * self.dim
        for gram in self._ngrams(text):
            bag[self._bucket(gram, self.dim)] += 1.0
        return l2_normalize(bag)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]
