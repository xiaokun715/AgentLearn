"""向量相似度计算（设计说明书 §14）。

Embedding 场景下使用 Cosine Similarity：
  cos(A,B) = A·B / (|A|·|B|)，范围 [-1, 1]，越接近 1 越相似。

0.98 → 非常相似 | 0.90 → 比较相似 | 0.75 → 可能相关 | 0.50 → 基本不同
但不要直接认为 0.9 就永远是 HIT —— 阈值必须通过数据集评估（§16）。
"""
from __future__ import annotations

import numpy as np


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度。向量为空或零向量时返回 0.0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0.0:
        return 0.0
    return float(float(va @ vb) / denom)


def l2_normalize(v: list[float]) -> list[float]:
    """L2 归一化：把向量变成单位向量（相似度退化为点积）。"""
    arr = np.asarray(v, dtype=np.float64)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        return [0.0] * len(v)
    return (arr / norm).tolist()
