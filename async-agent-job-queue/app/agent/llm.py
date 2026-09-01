"""MockLLM —— 无真实 API Key 也能演示完整 Agent 执行（语义缓存项目同思路）。

支持注入可重试错误，便于演示 Retry / Backoff（§42）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..domain.exceptions import RetryableError


class MockLLM:
    def __init__(self, latency: float = 0.03, fail_first: int = 0) -> None:
        self.latency = latency
        self.fail_first = fail_first
        self.calls = 0

    async def generate(self, prompt: str, **kwargs: Any) -> str:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RetryableError("mock LLM timeout (injected)")
        await asyncio.sleep(self.latency)
        return f"[mock-llm {self.calls}] {prompt[:80]}"
