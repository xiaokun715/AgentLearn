"""API 请求/响应模型（设计说明书 §8-10）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateJobRequest(BaseModel):
    agent: str = Field(..., description="Agent 注册名，如 research_agent / chaos_agent")
    input: dict[str, Any] = Field(default_factory=dict, description="Agent 输入")
    tenant_id: str = "default"
    priority: int = 0
    max_retries: int | None = Field(default=None, description="覆盖默认重试次数（3）")


class CreateJobResponse(BaseModel):
    job_id: str
    status: str


class CancelJobResponse(BaseModel):
    job_id: str
    status: str
    note: str | None = None


class DlqRetryResponse(BaseModel):
    job_id: str
    status: str
