"""实验一：Exact Cache vs Semantic Cache（设计说明书 §53 实验一）。

  Query A: 什么是TCP？
  Query B: TCP是什么？

字符串匹配（Redis 精确缓存）-> MISS；
语义缓存（Embedding + 相似度）-> HIT（Mock 向量 sim≈0.91）。

结论：普通缓存只能命中「完全相同」，Semantic Cache 命中「语义相同」。
"""
from __future__ import annotations

import asyncio
import hashlib

from semantic_cache.core.entry import ChatRequest, Message
from semantic_cache.embedding.mock import MockEmbeddingGenerator
from semantic_cache.factory import build_cache
from semantic_cache.llm.mock import MockLLM
from semantic_cache.search.base import cosine_similarity

QUERY_A = "什么是TCP？"
QUERY_B = "TCP是什么？"


def make(text: str) -> ChatRequest:
    return ChatRequest(user_id="u1", model="qwen", messages=[Message(role="user", content=text)], tenant_id="tenant-A")


def exact_key(text: str) -> str:
    """模拟普通 Redis 精确缓存：字符串指纹。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def main() -> None:
    # 1. Exact cache 视角
    print("=" * 60)
    print("实验一：Exact vs Semantic")
    print("=" * 60)
    print("\n[普通缓存 Exact Match]")
    print(f"  A = {QUERY_A!r:20s} -> key {exact_key(QUERY_A)[:16]}...")
    print(f"  B = {QUERY_B!r:20s} -> key {exact_key(QUERY_B)[:16]}...")
    print(f"  结果：{'相同' if exact_key(QUERY_A) == exact_key(QUERY_B) else '不同，无法命中'}")

    # 2. Semantic Cache 视角
    cache = build_cache()
    llm = MockLLM(delay=0.0)
    emb = MockEmbeddingGenerator()

    va = await emb.embed(QUERY_A)
    vb = await emb.embed(QUERY_B)
    sim = cosine_similarity(va, vb)

    resp = await llm.chat(make(QUERY_A))
    await cache.set(make(QUERY_A), resp)

    r = await cache.get(make(QUERY_B))
    print("\n[语义缓存 Semantic Match]")
    print(f"  cos(A, B) = {sim:.4f}")
    print(f"  Query B 结果：{'HIT' if r.hit else 'MISS'} (source={r.source})")

    print("\n结论：Exact 解决「字符串相同」，Semantic 解决「语义相同」。")


if __name__ == "__main__":
    asyncio.run(main())
