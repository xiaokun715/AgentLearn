"""Prompt API（设计说明书 §30 / §7~§8）。

  POST  /v1/prompts                       创建 Prompt 实体
  GET   /v1/prompts                       列出所有 Prompt
  POST  /v1/prompts/{name}/versions       追加不可变版本（v = max+1）
  GET   /v1/prompts/{name}/versions       列出版本
  GET   /v1/prompts/{name}/versions/{v}   获取单个版本
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .schemas import CreatePromptRequest, CreatePromptVersionRequest

router = APIRouter(prefix="/v1/prompts", tags=["prompts"])


@router.post("", status_code=201)
async def create_prompt(body: CreatePromptRequest, request: Request):
    rt = request.app.state.runtime
    prompt = await rt.prompt_registry.create_prompt(body.name, created_by=body.created_by)
    return prompt.to_dict()


@router.get("")
async def list_prompts(request: Request):
    rt = request.app.state.runtime
    prompts = await rt.prompt_registry.list_prompts()
    return [p.to_dict() for p in prompts]


@router.post("/{name}/versions", status_code=201)
async def create_prompt_version(name: str, body: CreatePromptVersionRequest, request: Request):
    rt = request.app.state.runtime
    pv = await rt.prompt_registry.create_version(
        name,
        template=body.template,
        variables=body.variables,
        metadata=body.metadata,
        created_by=body.created_by,
    )
    return pv.to_dict()


@router.get("/{name}/versions")
async def list_versions(name: str, request: Request):
    rt = request.app.state.runtime
    versions = await rt.prompt_registry.list_versions(name)
    return [v.to_dict(include_template=False) for v in versions]


@router.get("/{name}/versions/{version}")
async def get_version(name: str, version: int, request: Request):
    rt = request.app.state.runtime
    pv = await rt.prompt_registry.require_version(name, version)
    return pv.to_dict()
