"""实验三：TTL（设计说明书 §53 实验三 / §22 ~ §23）。

TTL = 5 秒：
  Request -> Cache HIT
  等待 5s
  Request -> Cache MISS

为什么 Agent 特别需要 TTL（§23）：
「当前服务器状态是什么？」昨天 healthy、今天 down，
命中旧缓存会让 Agent 基于过期数据做错误决策。
=> 语义相似 ≠ 语义有效。
"""
from __future__ import annotations

import asyncio
import time

from semantic_cache.core.entry import ChatRequest, Message
from semantic_cache.factory import build_cache
from semantic_cache.llm.mock import MockLLM

TTL_SECONDS = 5


def make(text: str) -> ChatRequest:
    return ChatRequest(user_id="u1", model="qwen", messages=[Message(role="user", content=text)], tenant_id="tenant-A")


async def main() -> None:
    cache = build_cache()
    llm = MockLLM(delay=0.0)

    print("=" * 60)
    print(f"实验三：TTL = {TTL_SECONDS}s")
    print("=" * 60)

    q = make("当前服务器状态是什么？")
    print(f"\n[0s] 第一次请求 -> 查询缓存")

    r = await cache.get(q)
    if not r.hit:
        resp = await llm.chat(q)
        await cache.set(q, resp, ttl=TTL_SECONDS)
        print("  MISS -> 调用 LLM -> 写入缓存（TTL=5s）")

    r = await cache.get(q)
    print(f"      再次请求 -> {'HIT' if r.hit else 'MISS'}")

    print(f"\n[等待 {TTL_SECONDS}s 让缓存过期]")
    await asyncio.sleep(TTL_SECONDS + 0.2)

    r = await cache.get(q)
    print(f"[{TTL_SECONDS:.0f}s] 缓存已过期 -> {'HIT' if r.hit else 'MISS'}")

    print("\n结论：TTL 到期后缓存自动失效，避免返回过期答案（§22）。")


if __name__ == "__main__":
    asyncio.run(main())
