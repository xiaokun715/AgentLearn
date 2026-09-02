"""统一 GuardrailContext 与阶段（设计说明书 §6）。

所有 Guardrail（Input / Context / Tool / Tool Result / Output）都处理同一份
数据结构；``stage`` 决定同一 Finding 采用哪条策略（§14 关键设计）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Stage(str, Enum):
    """安全边界对应的阶段（§6 stage 列表 + §14 policy 分阶段）。"""

    INPUT = "input"              # 用户输入
    CONTEXT = "context"          # 进入 LLM 的外部内容（RAG/记忆/历史）
    TOOL_CALL = "tool_call"      # Agent 请求调用某个 Tool
    TOOL_RESULT = "tool_result"  # Tool 返回的外部不可信结果
    OUTPUT = "output"            # LLM / Agent 输出

    def __str__(self) -> str:  # keep yaml keys uppercase, e.g. INPUT
        return self.name


@dataclass
class GuardrailContext:
    """一次安全检查的上下文。Detector 只读它，Action 改写 ``content``。"""

    request_id: str
    tenant_id: str
    user_id: str
    agent: str

    stage: Stage

    content: Any = None

    metadata: dict[str, Any] = field(default_factory=dict)

    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None
    tool_result: Any | None = None

    @property
    def text(self) -> str:
        """把 content 规整成可扫描的文本（str / dict / list 都支持）。"""
        return coerce_text(self.content)


def coerce_text(content: Any) -> str:
    """确定性规整：str 原样，dict/list 转 JSON，其余 str()。"""
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


__all__ = ["Stage", "GuardrailContext", "coerce_text"]
