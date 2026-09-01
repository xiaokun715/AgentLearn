"""Config Registry —— Agent Config 的不可变版本管理（设计说明书 §9~§11）。

一个 AgentConfig 是「model + parameters + prompt 引用 + tools + guardrails」
的组合 Snapshot。和 Prompt 一样不可变：要改就创建 vN+1。
"""
from __future__ import annotations

from typing import Any

from ..audit.audit_service import AuditService
from ..domain.audit import AuditAction
from ..domain.config import AgentConfig, GenerationParameters, ModelConfig, PromptRef
from ..domain.exceptions import NotFoundError
from ..storage.repository import RegistryRepository
from .prompt_registry import PromptRegistry


class ConfigRegistry:
    def __init__(
        self,
        repo: RegistryRepository,
        prompt_registry: PromptRegistry,
        audit: AuditService,
    ) -> None:
        self._repo = repo
        self._prompts = prompt_registry
        self._audit = audit

    async def create_config(
        self,
        agent: str,
        *,
        model: ModelConfig | dict | None = None,
        parameters: GenerationParameters | dict | None = None,
        prompt: PromptRef | dict | None = None,
        tools: dict[str, Any] | None = None,
        guardrails: dict[str, Any] | None = None,
        created_by: str = "",
    ) -> AgentConfig:
        """追加一个不可变 Config 版本。version 自动 = 当前 max + 1。

        校验：引用的 Prompt 版本必须真实存在 —— 避免发布"指向空气"的配置。
        """
        prompt_ref = self._coerce_prompt_ref(prompt)
        if prompt_ref is None:
            raise ValueError("config 必须引用一个 prompt（{name, version}）")

        # 校验 prompt 版本存在（§10：Config v8 引用 Prompt v13，v13 必须存在）
        await self._prompts.require_version(prompt_ref.name, prompt_ref.version)

        version = await self._repo.next_config_version(agent)
        config = AgentConfig(
            agent_name=agent,
            version=version,
            model=self._coerce(model, ModelConfig),
            parameters=self._coerce(parameters, GenerationParameters),
            prompt=prompt_ref,
            tools=tools or {"version": 1},
            guardrails=guardrails or {"version": 1},
            created_by=created_by,
        )
        await self._repo.create_config(config)
        await self._audit.record(
            created_by,
            AuditAction.CREATE_CONFIG,
            resource_type="agent_config",
            resource_id=str(config),
            after={"version": config.version, "prompt": prompt_ref.to_dict(),
                   "model": config.model.to_dict(), "tools": config.tools,
                   "guardrails": config.guardrails},
        )
        return config

    async def get_config(self, agent: str, version: int) -> AgentConfig | None:
        return await self._repo.get_config(agent, version)

    async def require_config(self, agent: str, version: int) -> AgentConfig:
        cfg = await self._repo.get_config(agent, version)
        if cfg is None:
            raise NotFoundError(f"Config {agent} 的版本 v{version} 不存在")
        return cfg

    async def list_configs(self, agent: str) -> list[AgentConfig]:
        return await self._repo.list_configs(agent)

    @staticmethod
    def _coerce(value: Any, cls: type) -> Any:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        return cls.from_dict(value)

    @staticmethod
    def _coerce_prompt_ref(value: PromptRef | dict | None) -> PromptRef | None:
        if value is None:
            return None
        if isinstance(value, PromptRef):
            return value
        return PromptRef.from_dict(value)
