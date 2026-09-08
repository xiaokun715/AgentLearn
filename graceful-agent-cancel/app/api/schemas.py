"""API 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SubmitRequest(BaseModel):
    query: str = Field(..., description="用户想查的问题", examples=["查一下最近两天的账单和天气"])
    city: str = "上海"
    days: int = Field(2, ge=1, le=7)
    agent: str = "research_agent"


class CancelRequest(BaseModel):
    mode: str | None = Field(
        default=None,
        description='"force"=掐断I/O（层2+层3一起）; "cooperative"=只标记，Agent 在动作边界跳出（仅层2）',
        examples=["force"],
    )
