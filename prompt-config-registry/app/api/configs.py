"""Agent Config API（设计说明书 §9~§11 / §30）。

  POST  /v1/agents/{agent}/configs             追加不可变 Config 版本（v = max+1）
  GET   /v1/agents/{agent}/configs             列出版本
  GET   /v1/agents/{agent}/configs/{version}   获取单个版本
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..domain.config import GenerationParameters, ModelConfig, PromptRef
from .schemas import CreateConfigRequest

router = APIRouter(prefix="/v1/agents", tags=["configs"])


@router.post("/{agent}/configs", status_code=201)
async def create_config(agent: str, body: CreateConfigRequest, request: Request):
    rt = request.app.state.runtime
    config = await rt.config_registry.create_config(
        agent,
        model=ModelConfig(provider=body.model.provider, name=body.model.name),
        parameters=GenerationParameters(
            temperature=body.parameters.temperature,
            top_p=body.parameters.top_p,
            max_tokens=body.parameters.max_tokens,
        ),
        prompt=PromptRef(name=body.prompt.name, version=body.prompt.version),
        tools=body.tools,
        guardrails=body.guardrails,
        created_by=body.created_by,
    )
    return config.to_dict()


@router.get("/{agent}/configs")
async def list_configs(agent: str, request: Request):
    rt = request.app.state.runtime
    configs = await rt.config_registry.list_configs(agent)
    return [c.to_dict() for c in configs]


@router.get("/{agent}/configs/{version}")
async def get_config(agent: str, version: int, request: Request):
    rt = request.app.state.runtime
    config = await rt.config_registry.require_config(agent, version)
    return config.to_dict()
