"""API 请求/响应模型（设计说明书 §30~§32）。

内容可能为文本，也可能是结构化对象（§26 Output 不只是文本），故 ``content`` 为 Any。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ContentCheckRequest(BaseModel):
    """Input / Context / Tool Result 共用请求体。"""

    content: Any
    agent: str = Field(default="default", max_length=128)
    user_id: str = Field(default="anonymous", max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InputCheckRequest(ContentCheckRequest):
    """POST /v1/guardrails/input（§30）。"""


class ContextCheckRequest(ContentCheckRequest):
    """POST /v1/guardrails/context（§24）。"""


class ToolResultCheckRequest(ContentCheckRequest):
    tool_name: str | None = Field(default=None, max_length=128)


class OutputCheckRequest(BaseModel):
    """POST /v1/guardrails/output（§32）。

    ``schema`` 提供时会对 JSON 输出做结构校验（不符合 -> RETRY，Demo 8）。
    字段内部命名为 ``json_schema`` 以避免与 Pydantic BaseModel.schema 冲突，
    对外仍用 ``schema`` 作为请求字段名（validation_alias）。
    """

    content: Any
    agent: str = Field(default="default", max_length=128)
    user_id: str = Field(default="anonymous", max_length=128)
    json_schema: dict[str, Any] | None = Field(
        default=None,
        validation_alias="schema",
        serialization_alias="schema",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCheckRequest(BaseModel):
    """POST /v1/guardrails/tool（§31）。"""

    agent: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] | None = None
    user_id: str = Field(default="anonymous", max_length=128)


class ApprovalDecisionRequest(BaseModel):
    decided_by: str = Field(default="human", max_length=128)
    note: str = Field(default="", max_length=512)


__all__ = [
    "InputCheckRequest",
    "ContextCheckRequest",
    "ToolResultCheckRequest",
    "OutputCheckRequest",
    "ToolCheckRequest",
    "ApprovalDecisionRequest",
]
