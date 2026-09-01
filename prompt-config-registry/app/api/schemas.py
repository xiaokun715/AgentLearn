"""API 请求模型（Pydantic）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CreatePromptRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    created_by: str = ""


class CreatePromptVersionRequest(BaseModel):
    template: str
    variables: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    created_by: str = ""


class ModelIn(BaseModel):
    provider: str = "qwen"
    name: str = "qwen3.5-27b"


class ParametersIn(BaseModel):
    temperature: float = 0.2
    top_p: float | None = None
    max_tokens: int | None = None


class PromptRefIn(BaseModel):
    name: str
    version: int = Field(ge=1)


class CreateConfigRequest(BaseModel):
    model: ModelIn = Field(default_factory=ModelIn)
    parameters: ParametersIn = Field(default_factory=ParametersIn)
    prompt: PromptRefIn
    tools: dict = Field(default_factory=lambda: {"version": 1})
    guardrails: dict = Field(default_factory=lambda: {"version": 1})
    created_by: str = ""


class DeployRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)
    traffic_percent: int = Field(default=100, ge=0, le=100)
    experiment: str | None = None
    created_by: str = ""
    reason: str = ""


class RolloutRequest(BaseModel):
    version: int = Field(ge=1)
    traffic_percent: int = Field(ge=0, le=100)
    created_by: str = ""
    reason: str = ""


class RollbackRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    created_by: str = ""
    reason: str = ""
