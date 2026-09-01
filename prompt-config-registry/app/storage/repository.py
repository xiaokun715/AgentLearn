"""Repository 接口 —— 存储层抽象（设计说明书 §26~§29）。

三种后端（memory / sqlite / postgres）都实现这个接口，
业务层只依赖抽象，不感知具体存储。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..domain.audit import AuditEntry
from ..domain.config import AgentConfig
from ..domain.deployment import Deployment
from ..domain.prompt import Prompt, PromptVersion


class RegistryRepository(ABC):
    # ---- prompts ----------------------------------------------------------
    @abstractmethod
    async def create_prompt(self, prompt: Prompt) -> None: ...

    @abstractmethod
    async def get_prompt(self, name: str) -> Prompt | None: ...

    @abstractmethod
    async def list_prompts(self) -> list[Prompt]: ...

    # ---- prompt_versions --------------------------------------------------
    @abstractmethod
    async def add_prompt_version(self, pv: PromptVersion) -> None: ...

    @abstractmethod
    async def get_prompt_version(self, name: str, version: int) -> PromptVersion | None: ...

    @abstractmethod
    async def get_prompt_version_by_id(self, id: str) -> PromptVersion | None: ...

    @abstractmethod
    async def list_prompt_versions(self, name: str) -> list[PromptVersion]: ...

    @abstractmethod
    async def next_prompt_version(self, name: str) -> int: ...

    # ---- agent_configs ----------------------------------------------------
    @abstractmethod
    async def create_config(self, config: AgentConfig) -> None: ...

    @abstractmethod
    async def get_config(self, agent: str, version: int) -> AgentConfig | None: ...

    @abstractmethod
    async def list_configs(self, agent: str) -> list[AgentConfig]: ...

    @abstractmethod
    async def next_config_version(self, agent: str) -> int: ...

    # ---- deployments ------------------------------------------------------
    @abstractmethod
    async def upsert_deployment(self, dep: Deployment) -> None: ...

    @abstractmethod
    async def get_deployment(self, agent: str, environment: str) -> Deployment | None: ...

    @abstractmethod
    async def get_deployment_by_id(self, id: str) -> Deployment | None: ...

    @abstractmethod
    async def list_deployments(self) -> list[Deployment]: ...

    # ---- audit_logs -------------------------------------------------------
    @abstractmethod
    async def append_audit(self, entry: AuditEntry) -> AuditEntry: ...

    @abstractmethod
    async def list_audit(
        self, *, limit: int = 100, action: str | None = None, agent: str | None = None
    ) -> list[AuditEntry]: ...

    # ---- lifecycle --------------------------------------------------------
    async def close(self) -> None:
        return None
