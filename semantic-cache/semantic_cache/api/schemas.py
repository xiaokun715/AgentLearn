"""HTTP 请求/响应模型（设计说明书 §3 / §48）。

POST /v1/chat 的请求体与 §3 保持一致，扩展了 Semantic Cache 所需的
租户 / 知识库版本 / Agent scope / 实时性等字段。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class MessageIn(BaseModel):
    role: str
    content: str


class ChatRequestIn(BaseModel):
    """对应 §3 Request。"""

    user_id: str = "user-001"
    model: str = "qwen"
    messages: list[MessageIn]
    temperature: float = 0.0
    tenant_id: str = "default"
    namespace: str | None = None
    tools: list[dict] | None = None
    knowledge_version: str | None = None
    agent_type: str | None = None
    task_type: str | None = None
    context_version: str | None = None
    time_sensitive: bool = False


class InvalidateRequest(BaseModel):
    """主动失效请求（§24 / §25）。"""

    cache_id: str | None = None
    namespace: str | None = None
    tenant_id: str | None = None
    model: str | None = None
    knowledge_version: str | None = None
    agent_type: str | None = None
    task_type: str | None = None
