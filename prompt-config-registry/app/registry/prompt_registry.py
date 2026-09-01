"""Prompt Registry —— 存"有哪些 Prompt、哪些版本"（设计说明书 §7~§8）。

核心不变量：**版本不可变**。本类只有 append 操作，没有任何 update / delete。
想改内容？只能创建新版本 vN+1 —— 这就是 Immutable Version（§38）。
"""
from __future__ import annotations

import uuid
from typing import Any

from ..audit.audit_service import AuditService
from ..domain.audit import AuditAction
from ..domain.exceptions import NotFoundError
from ..domain.prompt import Prompt, PromptVersion
from ..storage.repository import RegistryRepository


class PromptRegistry:
    def __init__(self, repo: RegistryRepository, audit: AuditService) -> None:
        self._repo = repo
        self._audit = audit

    async def create_prompt(self, name: str, *, created_by: str = "") -> Prompt:
        """创建一个新的 Prompt 实体（不含内容）。"""
        prompt = Prompt(id=str(uuid.uuid4()), name=name, created_by=created_by)
        await self._repo.create_prompt(prompt)
        await self._audit.record(
            created_by,
            AuditAction.CREATE_PROMPT,
            resource_type="prompt",
            resource_id=name,
            after={"name": name},
        )
        return prompt

    async def require_prompt(self, name: str) -> Prompt:
        prompt = await self._repo.get_prompt(name)
        if prompt is None:
            raise NotFoundError(f"Prompt '{name}' 不存在")
        return prompt

    async def create_version(
        self,
        name: str,
        *,
        template: str,
        variables: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        created_by: str = "",
    ) -> PromptVersion:
        """追加一个不可变版本。version 自动 = 当前 max + 1。"""
        await self.require_prompt(name)
        version = await self._repo.next_prompt_version(name)
        pv = PromptVersion(
            id=str(uuid.uuid4()),
            prompt_name=name,
            version=version,
            template=template,
            variables=variables or [],
            metadata=metadata or {},
            created_by=created_by,
        )
        await self._repo.add_prompt_version(pv)
        await self._audit.record(
            created_by,
            AuditAction.CREATE_PROMPT_VERSION,
            resource_type="prompt_version",
            resource_id=str(pv),
            after={"version": pv.version, "variables": pv.variables,
                   "metadata": pv.metadata, "template_length": len(pv.template)},
        )
        return pv

    async def get_version(self, name: str, version: int) -> PromptVersion | None:
        return await self._repo.get_prompt_version(name, version)

    async def require_version(self, name: str, version: int) -> PromptVersion:
        pv = await self._repo.get_prompt_version(name, version)
        if pv is None:
            raise NotFoundError(f"Prompt {name} 的版本 v{version} 不存在")
        return pv

    async def list_prompts(self) -> list[Prompt]:
        return await self._repo.list_prompts()

    async def list_versions(self, name: str) -> list[PromptVersion]:
        await self.require_prompt(name)
        return await self._repo.list_prompt_versions(name)
