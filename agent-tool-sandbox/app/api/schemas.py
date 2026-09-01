"""API 请求 / 响应模型（设计说明书 §7）。

核心契约：
    POST /v1/executions  {type, code, policy?}  →  {execution_id, status: queued}
    GET  /v1/executions/{id}                    →  {execution_id, status, stdout, stderr, exit_code, duration_ms, ...}
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MAX_CODE_BYTES = 64 * 1024


class PolicyRequest(BaseModel):
    """Agent 的「能力请求」。注意：请求 ≠ 权限（§10），最终由服务端 Policy 决定。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    memory_mb: int | None = Field(default=None, ge=16, le=8192)
    cpu: float | None = Field(default=None, ge=0.1, le=8.0)
    network: bool | None = None


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(alias="type", description="python | shell | node | sql")
    code: str = Field(min_length=1, max_length=MAX_CODE_BYTES)
    policy: PolicyRequest | None = None


class ExecutionCreated(BaseModel):
    execution_id: str
    status: str


class ExecutionView(BaseModel):
    execution_id: str
    status: str
    policy_id: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str | None = None
    duration_ms: int | None = None
    resource_usage: dict = {}
    created_at: str | None = None
