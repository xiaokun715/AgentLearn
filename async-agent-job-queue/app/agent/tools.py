"""Tool 抽象（设计说明书 §21-23）。

Tool 调用结果必须在 Checkpoint 中留下 Tool Execution Record，
恢复时才不会重复执行、重复花钱 / 产生副作用。
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    name: str = ""
    description: str = ""

    @abstractmethod
    async def run(self, **kwargs: Any) -> Any:
        ...


def make_tool_call_id(tool_name: str, kwargs: dict) -> str:
    """基于 工具名 + 参数 生成确定性的 tool_call_id。

    同一参数集的调用会得到同一个 ID —— 这正是幂等去重的依据。
    """
    payload = json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]
    return f"{tool_name}:{digest}"


class SearchWebTool(Tool):
    """只读的模拟搜索引擎。幂等：相同 query 返回相同结果。"""

    name = "search_web"
    description = "模拟搜索：根据 query 返回若干条结果"

    def __init__(self, latency: float = 0.03, top_k: int = 3) -> None:
        self.latency = latency
        self.top_k = top_k

    async def run(self, query: str, top_k: int = 3) -> list[str]:
        await _sleep(self.latency)
        results = [
            f"[{i}] 关于 “{query}” 的模拟搜索结果 #{i}：来自可信来源的片段。"
            for i in range(1, max(1, min(int(top_k), self.top_k)) + 1)
        ]
        return results


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
