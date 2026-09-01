"""实验四：Agent Cache（设计说明书 §53 实验四 / §37 ~ §39）。

模拟三个 Agent 提交相似子任务，观察：
  Cache Hit Rate / Token Saved / Cost Saved / Latency Saved

  Agent 1: "分析这个 TCP timeout"
  Agent 2: "分析 TCP timeout 的原因"
  Agent 3: "请分析 TCP timeout 的原因"

Agent 为什么特别适合 Semantic Cache（§37）：任务型请求产生大量重复/相似子任务；
但必须区分 Semantic Similarity 与 Business Identity（§38 ~ §39）。
"""
from __future__ import annotations

import asyncio
import time

from semantic_cache.core.entry import ChatRequest, Message
from semantic_cache.factory import build_cache
from semantic_cache.llm.mock import MockLLM

AGENT_TASKS = [
    ("fault_diagnosis", "分析这个 TCP timeout"),
    ("fault_diagnosis", "分析 TCP timeout 的原因"),
    ("fault_diagnosis", "请分析 TCP timeout 的原因"),
]


def make(agent_type: str, task: str) -> ChatRequest:
    return ChatRequest(
        user_id="agent-sys",
        model="qwen",
        messages=[
            Message(role="system", content=f"你是{agent_type}助手，输出分析结论。"),
            Message(role="user", content=task),
        ],
        tenant_id="tenant-A",
        namespace="agent-tasks",
        agent_type=agent_type,
        task_type=agent_type,
        context_version="v42",
    )


async def main() -> None:
    cache = build_cache()
    llm = MockLLM(delay=1.0)

    print("=" * 64)
    print("实验四：Agent Cache（两轮任务）")
    print("=" * 64)

    for round_no in (1, 2):
        print(f"\n--- 第 {round_no} 轮 ---")
        for agent_type, task in AGENT_TASKS:
            req = make(agent_type, task)
            started = time.perf_counter()
            r = await cache.get(req)
            if r.hit:
                ms = (time.perf_counter() - started) * 1000
                print(f"  [HIT  sim={r.similarity:.4f}] {task}  ({ms:6.1f} ms)")
            else:
                resp = await llm.chat(req)
                await cache.set(req, resp)
                ms = (time.perf_counter() - started) * 1000
                print(f"  [MISS            ] {task}  ({ms:6.0f} ms)")

    stats = await cache.stats()
    print("\n" + "=" * 64)
    print("观察指标（§42 ~ §46）")
    print("=" * 64)
    print(f"  Cache Hit Rate : {stats['hit_rate']:.0%}")
    print(f"  Tokens Saved   : {stats['tokens_saved']}  (每次命中避免 ~200 tokens)")
    print(f"  Cost Saved     : ${stats['cost_saved_usd']:.6f}  (Input $1 / Output $5 per 1M)")
    print(f"  Avg Hit Latency: {stats['avg_latency_ms']:.1f} ms  (LLM 生成 ~1000 ms)")


if __name__ == "__main__":
    asyncio.run(main())
