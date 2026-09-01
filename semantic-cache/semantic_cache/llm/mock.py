"""Mock LLM（设计说明书 §51）。

Demo 第一阶段不接真实 LLM：
  - 睡 ``delay`` 秒模拟生成耗时（默认 1s，用于直观对比 HIT 10ms vs MISS 1s）
  - 返回带 usage 的 OpenAI 风格响应，供 §44 Token Saving / §45 Cost Saving 计算
"""
from __future__ import annotations

import asyncio
import time

from ..core.entry import ChatRequest


class MockLLM:
    def __init__(self, *, delay: float = 1.0, input_tokens: int = 120, output_tokens: int = 80):
        self.delay = delay
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    async def chat(self, request: ChatRequest) -> dict:
        """模拟一次 LLM 调用，返回 OpenAI 兼容的 chat.completion 结构。"""
        await asyncio.sleep(self.delay)
        answer = f"Mock LLM answer for: {request.user_text}"
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": request.model,
            "created": int(time.time()),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": self.input_tokens,
                "completion_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
            },
        }
