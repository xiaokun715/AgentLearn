"""实验二：Threshold 评估（设计说明书 §53 实验二 / §16）。

准备正负样本对 -> 计算相似度 -> 在不同阈值下评估
  Precision / Recall / Hit Rate / False Hit Rate
找到合适的 Operating Point。

为什么不能简单写死 0.9（§14 / §16）：
阈值必须由「数据集 + 指标」决定，不同领域（技术问答/客服/医疗/Agent）语义分布不同。
"""
from __future__ import annotations

import asyncio

from semantic_cache.embedding.mock import MockEmbeddingGenerator
from semantic_cache.search.base import cosine_similarity

# 正样本对：语义相同 / 高度相似（应 HIT）
POSITIVE: list[tuple[str, str]] = [
    ("什么是TCP？", "TCP是什么？"),
    ("什么是TCP协议？", "TCP协议是什么？"),
    ("TCP是干什么的？", "TCP的作用是什么？"),
    ("什么是HTTP？", "HTTP是什么？"),
    ("什么是UDP？", "UDP协议是什么？"),
    ("如何提升缓存命中率？", "怎么提高缓存命中率？"),
    ("数据库索引是什么", "什么是数据库索引"),
    ("解释一下线程和进程", "线程和进程的区别是什么"),
    ("什么是死锁", "死锁是什么"),
    ("介绍一下微服务", "微服务是什么"),
    ("什么是数据库事务", "事务是什么"),
    ("Redis持久化有哪几种", "Redis的持久化方式"),
]

# 负样本对：相关但不同 / 完全不同（应 MISS）
NEGATIVE: list[tuple[str, str]] = [
    ("什么是TCP？", "什么是UDP？"),
    ("什么是TCP协议？", "什么是HTTP协议？"),
    ("什么是TCP？", "今天天气怎么样？"),
    ("如何提升缓存命中率？", "数据库索引怎么建？"),
    ("什么是死锁", "什么是信号量"),
    ("什么是进程", "什么是线程"),
    ("Redis是什么", "Kafka是什么"),
    ("MySQL索引原理", "MySQL主从同步"),
    ("TCP三次握手", "TCP拥塞控制"),
    ("写一首诗", "写一段代码"),
    ("推荐一部电影", "明天会议几点"),
    ("介绍一下上海", "怎么申请护照"),
]

THRESHOLDS = [0.70, 0.75, 0.80, 0.83, 0.85, 0.88, 0.90, 0.92, 0.95]


async def build_similarities() -> tuple[list[float], list[float]]:
    emb = MockEmbeddingGenerator()
    pos: list[float] = []
    for a, b in POSITIVE:
        va, vb = await emb.embed(a), await emb.embed(b)
        pos.append(cosine_similarity(va, vb))
    neg: list[float] = []
    for a, b in NEGATIVE:
        va, vb = await emb.embed(a), await emb.embed(b)
        neg.append(cosine_similarity(va, vb))
    return pos, neg


def evaluate(pos: list[float], neg: list[float], threshold: float) -> dict[str, float]:
    tp = sum(1 for s in pos if s >= threshold)
    fp = sum(1 for s in neg if s >= threshold)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / len(pos)
    hit_rate = (tp + fp) / (len(pos) + len(neg))
    false_hit_rate = fp / len(neg)
    return {"precision": precision, "recall": recall, "hit_rate": hit_rate, "false_hit_rate": false_hit_rate}


async def main() -> None:
    pos, neg = await build_similarities()
    print("=" * 76)
    print("实验二：Threshold 评估")
    print("=" * 76)
    print(f"正样本对: {len(pos)} 条，相似度分布 [min={min(pos):.3f}, mean={sum(pos)/len(pos):.3f}, max={max(pos):.3f}]")
    print(f"负样本对: {len(neg)} 条，相似度分布 [min={min(neg):.3f}, mean={sum(neg)/len(neg):.3f}, max={max(neg):.3f}]")
    print()
    print(f"{'threshold':>10} | {'precision':>9} | {'recall':>6} | {'hit_rate':>8} | {'false_hit':>9} | 评价")
    print("-" * 76)
    for t in THRESHOLDS:
        m = evaluate(pos, neg, t)
        verdict = "平衡点" if 0.9 <= m["precision"] <= 1.0 and m["recall"] >= 0.6 else (
            "过严(MISS多)" if m["precision"] == 1.0 else "过松(风险高)" if m["false_hit_rate"] > 0.1 else ""
        )
        print(
            f"{t:>10.2f} | {m['precision']:>9.2%} | {m['recall']:>6.2%} | {m['hit_rate']:>8.2%} | {m['false_hit_rate']:>9.2%} | {verdict}"
        )
    print("\n观察：阈值升高 -> Precision 升 / Recall 降 / False Hit 降。")
    print("选择目标：False Positive 尽可能低，同时 Cache Hit Rate 足够高（§16）。")
    print("注意：这是 Mock Embedding 的结果；接入真实模型必须重新评估（§14）。")


if __name__ == "__main__":
    asyncio.run(main())
