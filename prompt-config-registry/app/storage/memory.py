"""内存版 Repository —— 零依赖，供测试 / Demo 使用。

语义与 SQLite / PostgreSQL 完全一致，便于三者在测试间切换。
"""
from __future__ import annotations

from ..domain.audit import AuditEntry
from ..domain.config import AgentConfig
from ..domain.deployment import Deployment
from ..domain.exceptions import ConflictError
from ..domain.prompt import Prompt, PromptVersion
from .repository import RegistryRepository


class MemoryRepository(RegistryRepository):
    def __init__(self) -> None:
        self._prompts: dict[str, Prompt] = {}
        self._prompt_versions: dict[str, list[PromptVersion]] = {}  # name -> [pv...]
        self._configs: dict[str, dict[int, AgentConfig]] = {}       # agent -> {version: cfg}
        self._deployments: dict[str, Deployment] = {}               # id -> dep
        self._deployment_keys: dict[tuple[str, str], str] = {}      # (agent, env) -> id
        self._audit: list[AuditEntry] = []

    # ---- prompts ----------------------------------------------------------
    async def create_prompt(self, prompt: Prompt) -> None:
        if prompt.name in self._prompts:
            raise ConflictError(f"Prompt '{prompt.name}' 已存在")
        self._prompts[prompt.name] = prompt

    async def get_prompt(self, name: str) -> Prompt | None:
        return self._prompts.get(name)

    async def list_prompts(self) -> list[Prompt]:
        return list(self._prompts.values())

    # ---- prompt_versions --------------------------------------------------
    async def add_prompt_version(self, pv: PromptVersion) -> None:
        versions = self._prompt_versions.setdefault(pv.prompt_name, [])
        if any(v.version == pv.version for v in versions):
            raise ConflictError(f"Prompt {pv.prompt_name} 的版本 v{pv.version} 已存在（不可变）")
        versions.append(pv)
        versions.sort(key=lambda v: v.version)

    async def get_prompt_version(self, name: str, version: int) -> PromptVersion | None:
        for v in self._prompt_versions.get(name, []):
            if v.version == version:
                return v
        return None

    async def get_prompt_version_by_id(self, id: str) -> PromptVersion | None:
        for versions in self._prompt_versions.values():
            for v in versions:
                if v.id == id:
                    return v
        return None

    async def list_prompt_versions(self, name: str) -> list[PromptVersion]:
        return list(self._prompt_versions.get(name, []))

    async def next_prompt_version(self, name: str) -> int:
        versions = self._prompt_versions.get(name, [])
        return (versions[-1].version + 1) if versions else 1

    # ---- agent_configs ----------------------------------------------------
    async def create_config(self, config: AgentConfig) -> None:
        bucket = self._configs.setdefault(config.agent_name, {})
        if config.version in bucket:
            raise ConflictError(f"Config {config.agent_name} 的版本 v{config.version} 已存在（不可变）")
        bucket[config.version] = config

    async def get_config(self, agent: str, version: int) -> AgentConfig | None:
        return self._configs.get(agent, {}).get(version)

    async def list_configs(self, agent: str) -> list[AgentConfig]:
        versions = self._configs.get(agent, {})
        return [versions[k] for k in sorted(versions)]

    async def next_config_version(self, agent: str) -> int:
        versions = self._configs.get(agent, {})
        return (max(versions) + 1) if versions else 1

    # ---- deployments ------------------------------------------------------
    async def upsert_deployment(self, dep: Deployment) -> None:
        existing_id = self._deployment_keys.get((dep.agent_name, dep.environment))
        if existing_id is not None and existing_id != dep.id:
            # 保持 (agent, env) 唯一：删除旧的 id 映射
            self._deployments.pop(existing_id, None)
        self._deployments[dep.id] = dep
        self._deployment_keys[(dep.agent_name, dep.environment)] = dep.id

    async def get_deployment(self, agent: str, environment: str) -> Deployment | None:
        id = self._deployment_keys.get((agent, environment))
        return self._deployments.get(id) if id else None

    async def get_deployment_by_id(self, id: str) -> Deployment | None:
        dep = self._deployments.get(id)
        if dep is None:
            return None
        return dep

    async def list_deployments(self) -> list[Deployment]:
        return list(self._deployments.values())

    # ---- audit_logs -------------------------------------------------------
    async def append_audit(self, entry: AuditEntry) -> AuditEntry:
        entry.id = len(self._audit) + 1
        self._audit.append(entry)
        return entry

    async def list_audit(
        self, *, limit: int = 100, action: str | None = None, agent: str | None = None
    ) -> list[AuditEntry]:
        rows = reversed(self._audit)  # 新的在前
        out: list[AuditEntry] = []
        for e in rows:
            if action and e.action != action:
                continue
            if agent and agent not in e.resource_id:
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out

    async def close(self) -> None:
        return None
