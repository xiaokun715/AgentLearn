"""Mock LLM Provider（设计说明书 §40）。

不要第一版就依赖真实 LLM。先用可注入失败/延迟/工具调用的 Mock，
才能稳定测试 TTFT、Backpressure、Disconnect、Reconnect、Failure。
"""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, List


class ProviderError(Exception):
    """上游 LLM 错误（如超时）。会被转为 error + done(reason=error) 事件。"""

    def __init__(self, code: str, *, retryable: bool = True, detail: str | None = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "retryable": self.retryable}
        if self.detail is not None:
            d["detail"] = self.detail
        return d


@dataclass(slots=True)
class ProviderEvent:
    """Provider 产出的内容事件（不含生命周期事件，后者由 Stream 合成）。"""
    type: str  # metadata / token / tool_call
    data: dict[str, Any] = field(default_factory=dict)
    output_tokens_inc: int = 0  # 该事件对 output_tokens 的贡献


class BaseProvider(ABC):
    input_tokens: int = 0  # 请求侧的输入 token 数（用于 usage 统计）

    @abstractmethod
    async def stream(self, request, *, start_token_index: int = 0) -> AsyncIterator[ProviderEvent]:
        """产出内容事件。

        start_token_index：断线重连时从第 N 个 token 继续（设计说明书 §49 验收场景）。
        """


class MockLLMProvider(BaseProvider):
    def __init__(
        self,
        tokens: List[str] | None = None,
        *,
        delay: float = 0.05,
        input_tokens: int = 120,
        fail_after: int | None = None,
        fail_code: str = "UPSTREAM_TIMEOUT",
        retryable: bool = True,
        tool_call_after: int | None = None,
    ):
        self.tokens = list(tokens) if tokens is not None else list("你好，这是一个 Streaming Demo。")
        self.delay = delay
        self.input_tokens = input_tokens
        self.fail_after = fail_after       # 第 N 个 token（含）之后抛错
        self.fail_code = fail_code
        self.retryable = retryable
        self.tool_call_after = tool_call_after  # 第 N 个 token 处插入 tool_call 事件

    async def stream(self, request, *, start_token_index: int = 0) -> AsyncIterator[ProviderEvent]:
        # 首次（从头）运行时发一条 metadata；重连续传时不重复发
        if start_token_index == 0:
            yield ProviderEvent(
                "metadata",
                {"model": "mock-llm-1", "input_tokens": self.input_tokens, "prompt": request.prompt},
            )
        for i in range(start_token_index, len(self.tokens)):
            await asyncio.sleep(self.delay)  # 模拟生成耗时
            if self.fail_after is not None and i >= self.fail_after:
                raise ProviderError(
                    self.fail_code, retryable=self.retryable,
                    detail=f"failed after {i} tokens",
                )
            if self.tool_call_after is not None and i == self.tool_call_after:
                yield ProviderEvent(
                    "tool_call",
                    {"name": "web_search", "arguments": json.dumps({"query": request.prompt[:40]}, ensure_ascii=False)},
                    output_tokens_inc=2,
                )
                continue
            yield ProviderEvent("token", {"delta": self.tokens[i]}, output_tokens_inc=1)
