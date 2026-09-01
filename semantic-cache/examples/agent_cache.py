"""Agent 子任务缓存 Demo（设计说明书 §37 ~ §39 / §53 实验四）。

多个 Agent 提交语义高度相似的子任务：
  Agent 1: "分析这个 TCP timeout"
  Agent 2: "分析 TCP timeout 的原因"
  Agent 3: "请分析 TCP timeout 的原因"

在 Agent Node 层命中缓存，避免重复调用 LLM。
注意 §38/§39 的约束：agent_type/task_type 必须一致才能复用答案，
「语义相似 ≠ Business Identity 相同」。
（注：Mock Embedding 是 n-gram 近似，同义句命中 ~0.85；真实 Embedding 可到 0.9+。）
"""
from __future__ import annotations

import asyncio
import time

from semantic_cache.core.entry import ChatRequest, Message
from semantic_cache.factory import build_cache
from semantic_cache.llm.mock import MockLLM


def agent_request(agent_type: str, task: str, *, context_version: str = "v42") -> ChatRequest:
    messages = [
        Message(role="system", content=f"你是{agent_type}助手，输出分析结论。"),
        Message(role="user", content=task),
    ]
    return ChatRequest(
        user_id="agent-sys",
        model="qwen",
        messages=messages,
        tenant_id="tenant-A",
        namespace="agent-tasks",  # Agent 缓存与普通 chat 缓存隔离（§38）
        agent_type=agent_type,
        task_type=agent_type,
        context_version=context_version,
    )


async def main() -> None:
    cache = build_cache()
    llm = MockLLM(delay=1.0)

    print("=" * 64)
    print("Agent Cache Demo（fault_diagnosis 子任务）")
    print("=" * 64)

    tasks = [
        ("fault_diagnosis", "分析这个 TCP timeout"),
        ("fault_diagnosis", "分析 TCP timeout 的原因"),
        ("fault_diagnosis", "请分析 TCP timeout 的原因"),
    ]

    for agent_type, task in tasks:
        req = agent_request(agent_type, task)
        started = time.perf_counter()
        result = await cache.get(req)
        if result.hit:
            ms = (time.perf_counter() - started) * 1000
            print(f"[HIT  sim={result.similarity:.4f}] {task}   ({ms:6.1f} ms)")
        else:
            resp = await llm.chat(req)
            await cache.set(req, resp)
            ms = (time.perf_counter() - started) * 1000
            print(f"[MISS            ] {task}   ({ms:6.0f} ms)")

    print()
    stats = await cache.stats()
    print("--- Agent Cache 效果 ---")
    print(f"  Hit Rate    : {stats['hit_rate']:.0%}")
    print(f"  Tokens Saved: {stats['tokens_saved']}")
    print(f"  Cost Saved  : ${stats['cost_saved_usd']:.6f}")
    print(f"  Avg Latency : {stats['avg_latency_ms']:.1f} ms")


if __name__ == "__main__":
    asyncio.run(main())
