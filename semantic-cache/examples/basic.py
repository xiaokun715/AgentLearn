"""基础 Demo（设计说明书 §51）。

运行：python examples/basic.py

演示完整流程：
  第一次  MISS -> LLM(1s) -> 写入缓存
  之后    HIT  -> 10ms 直接返回

并输出 Token Saved / Cost Saved / Latency Saved（§44 ~ §46）。
"""
from __future__ import annotations

import asyncio
import time

from semantic_cache.core.entry import ChatRequest, Message
from semantic_cache.factory import build_cache
from semantic_cache.llm.mock import MockLLM


def make(text: str, *, system: str | None = None) -> ChatRequest:
    messages = []
    if system:
        messages.append(Message(role="system", content=system))
    messages.append(Message(role="user", content=text))
    return ChatRequest(user_id="user-001", model="qwen", messages=messages, tenant_id="tenant-A")


async def main() -> None:
    cache = build_cache()
    llm = MockLLM(delay=1.0)  # §51：Mock LLM 睡 1 秒模拟生成耗时

    print("=" * 62)
    print("Semantic Cache Demo（Mock LLM / Mock Embedding）")
    print("=" * 62)

    queries = [
        "什么是TCP协议？",   # 冷启动 -> MISS
        "TCP协议是什么？",   # 同义表达 -> 语义命中（§51 示例，sim≈0.93）
        "什么是UDP协议？",   # 相关但不同 -> MISS（§17 错误命中的高危区）
        "今天天气怎么样？",   # 完全不同 -> MISS
    ]

    for q in queries:
        req = make(q)
        started = time.perf_counter()
        result = await cache.get(req)
        if result.hit:
            ms = (time.perf_counter() - started) * 1000
            print(f"[HIT  {result.source:8s} sim={result.similarity:.4f}] {q}   ({ms:6.1f} ms)")
        else:
            resp = await llm.chat(req)
            await cache.set(req, resp)
            ms = (time.perf_counter() - started) * 1000
            content = resp["choices"][0]["message"]["content"]
            print(f"[MISS            ] {q}   ({ms:6.0f} ms) -> {content!r}")

    print()
    print("--- Metrics（§41 ~ §46）---")
    stats = await cache.stats()
    for key in [
        "requests_total", "hits_total", "misses_total", "hit_rate",
        "tokens_saved", "cost_saved_usd", "avg_latency_ms",
    ]:
        print(f"  {key:20s}: {stats[key]}")


if __name__ == "__main__":
    asyncio.run(main())
