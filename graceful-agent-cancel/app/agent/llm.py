"""MockLLM —— 一个“真能被打断”的流式假大模型。

它用多个小 ``asyncio.sleep`` 模拟 LLM 一个一个字块往外吐（像真实 SSE 流式响应）。
因为每次等待都是可取消的协程 await，所以层3 的 ``asyncio.Task.cancel()``
能准确地在“等大模型吐字”的当下把它掐断 —— 这正是第十章所说的：
“强行中止对 OpenAI 的 HTTP 请求，省下 Token 计费”。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import AgentContext

_DEFAULT_THOUGHTS = [
    "先把用户诉求拆解成可执行的几个动作，然后逐一收集证据。",
    "已拿到账单与天气，可以开始整理结论了。",
    "这份网页内容较长，只提炼与查询最相关的关键信息。",
]


class MockLLM:
    def __init__(self, thoughts: list[str] | None = None) -> None:
        self._thoughts = thoughts or _DEFAULT_THOUGHTS
        self._idx = 0

    async def think(self, prompt: str, ctx: "AgentContext | None" = None) -> str:
        """返回一整段“思考结果”，但内部逐块流式生成、每块之间可被打断。"""
        cfg = ctx.config if ctx is not None else None
        chunk_delay = getattr(cfg, "llm_chunk_delay", 0.05) or 0.05
        chunks = getattr(cfg, "llm_chunks_per_think", 3) or 3

        text = self._thoughts[self._idx % len(self._thoughts)]
        self._idx += 1
        # 切成 chunks 逐块“吐出”，每块之间 await —— 取消点就在这里
        width = max(1, (len(text) + chunks - 1) // chunks)
        parts = [text[i:i + width] for i in range(0, len(text), width)] or [text]
        for part in parts:
            await asyncio.sleep(chunk_delay)
        return text
