"""PostgreSQL 版 Repository（可选后端，设计说明书 §26~§29）。

需要 ``pip install prompt-config-registry[postgres]`` 与运行中的 PostgreSQL
（见 docker-compose.yml，首次启动自动执行 migrations/001_create_tables.sql）。
未安装 asyncpg 或未配置 DATABASE_URL 时不会被加载。
"""
from __future__ import annotations

import uuid
from typing import Any

from ..domain.audit import AuditEntry
from ..domain.config import AgentConfig
from ..domain.deployment import Deployment
from ..domain.exceptions import ConflictError
from ..domain.prompt import Prompt, PromptVersion
from .models import dumps, from_db, loads, to_db
from .repository import RegistryRepository


class PostgresDatabase:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: Any = None

    async def connect(self) -> None:
        import asyncpg  # 延迟导入：未安装 asyncpg 时不报错

        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=8)

    @property
    def _acq(self) -> Any:
        return self.pool.acquire()

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()


class PostgresRepository(RegistryRepository):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    async def _execute(self, sql: str, *args: Any) -> None:
        async with self.db._acq as conn:
            await conn.execute(sql, *args)

    async def _fetchrow(self, sql: str, *args: Any) -> Any:
        async with self.db._acq as conn:
            return await conn.fetchrow(sql, *args)

    async def _fetch(self, sql: str, *args: Any) -> list[Any]:
        async with self.db._acq as conn:
            return await conn.fetch(sql, *args)

    # ---- prompts ----------------------------------------------------------
    async def create_prompt(self, prompt: Prompt) -> None:
        try:
            await self._execute(
                "INSERT INTO prompts (id, name, created_by, created_at) VALUES ($1,$2,$3,$4)",
                prompt.id, prompt.name, prompt.created_by, to_db(prompt.created_at),
            )
        except Exception as e:
            raise ConflictError(f"Prompt '{prompt.name}' 已存在") from e

    async def get_prompt(self, name: str) -> Prompt | None:
        row = await self._fetchrow("SELECT * FROM prompts WHERE name=$1", name)
        return self._row_to_prompt(row) if row else None

    async def list_prompts(self) -> list[Prompt]:
        rows = await self._fetch("SELECT * FROM prompts ORDER BY created_at")
        return [self._row_to_prompt(r) for r in rows]

    @staticmethod
    def _row_to_prompt(row: Any) -> Prompt:
        return Prompt(
            id=str(row["id"]),
            name=row["name"],
            created_by=row["created_by"] or "",
            created_at=from_db(row["created_at"]),
        )

    # ---- prompt_versions --------------------------------------------------
    async def add_prompt_version(self, pv: PromptVersion) -> None:
        prompt = await self.get_prompt(pv.prompt_name)
        if prompt is None:
            raise ConflictError(f"Prompt '{pv.prompt_name}' 不存在，请先创建")
        try:
            await self._execute(
                "INSERT INTO prompt_versions (id, prompt_id, version, template, variables, metadata, created_by, created_at)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                pv.id, prompt.id, pv.version, pv.template,
                loads(dumps(pv.variables)) if pv.variables else None,
                loads(dumps(pv.metadata)) if pv.metadata else None,
                pv.created_by, to_db(pv.created_at),
            )
        except Exception as e:
            raise ConflictError(
                f"Prompt {pv.prompt_name} 的版本 v{pv.version} 已存在（不可变，只能追加新版本）"
            ) from e

    async def get_prompt_version(self, name: str, version: int) -> PromptVersion | None:
        row = await self._fetchrow(
            """SELECT pv.*, p.name AS prompt_name FROM prompt_versions pv
               JOIN prompts p ON p.id = pv.prompt_id
               WHERE p.name=$1 AND pv.version=$2""",
            name, version,
        )
        return self._row_to_pv(row) if row else None

    async def get_prompt_version_by_id(self, id: str) -> PromptVersion | None:
        row = await self._fetchrow(
            """SELECT pv.*, p.name AS prompt_name FROM prompt_versions pv
               JOIN prompts p ON p.id = pv.prompt_id WHERE pv.id=$1""",
            id,
        )
        return self._row_to_pv(row) if row else None

    async def list_prompt_versions(self, name: str) -> list[PromptVersion]:
        rows = await self._fetch(
            """SELECT pv.*, p.name AS prompt_name FROM prompt_versions pv
               JOIN prompts p ON p.id = pv.prompt_id
               WHERE p.name=$1 ORDER BY pv.version""",
            name,
        )
        return [self._row_to_pv(r) for r in rows]

    async def next_prompt_version(self, name: str) -> int:
        row = await self._fetchrow(
            """SELECT COALESCE(MAX(pv.version), 0) + 1 AS n FROM prompt_versions pv
               JOIN prompts p ON p.id = pv.prompt_id WHERE p.name=$1""",
            name,
        )
        return int(row["n"])

    @staticmethod
    def _row_to_pv(row: Any) -> PromptVersion:
        return PromptVersion(
            id=str(row["id"]),
            prompt_name=row["prompt_name"],
            version=row["version"],
            template=row["template"],
            variables=loads(row["variables"]) or [],
            metadata=loads(row["metadata"]) or {},
            created_by=row["created_by"] or "",
            created_at=from_db(row["created_at"]),
        )

    # ---- agent_configs ----------------------------------------------------
    async def create_config(self, config: AgentConfig) -> None:
        try:
            await self._execute(
                "INSERT INTO agent_configs (id, agent_name, version, config, created_by, created_at)"
                " VALUES ($1,$2,$3,$4,$5,$6)",
                str(uuid.uuid4()), config.agent_name, config.version,
                loads(dumps(config.to_dict())), config.created_by, to_db(config.created_at),
            )
        except Exception as e:
            raise ConflictError(
                f"Config {config.agent_name} 的版本 v{config.version} 已存在（不可变，只能追加新版本）"
            ) from e

    async def get_config(self, agent: str, version: int) -> AgentConfig | None:
        row = await self._fetchrow(
            "SELECT * FROM agent_configs WHERE agent_name=$1 AND version=$2", agent, version
        )
        return self._row_to_config(row) if row else None

    async def list_configs(self, agent: str) -> list[AgentConfig]:
        rows = await self._fetch(
            "SELECT * FROM agent_configs WHERE agent_name=$1 ORDER BY version", agent
        )
        return [self._row_to_config(r) for r in rows]

    async def next_config_version(self, agent: str) -> int:
        row = await self._fetchrow(
            "SELECT COALESCE(MAX(version), 0) + 1 AS n FROM agent_configs WHERE agent_name=$1",
            agent,
        )
        return int(row["n"])

    @staticmethod
    def _row_to_config(row: Any) -> AgentConfig:
        data = dict(loads(row["config"]) or {})
        data["agent"] = data.get("agent", row["agent_name"])
        data["version"] = data.get("version", row["version"])
        data["created_by"] = data.get("created_by", row["created_by"])
        data["created_at"] = data.get("created_at", row["created_at"])
        return AgentConfig.from_dict(data)

    # ---- deployments ------------------------------------------------------
    async def upsert_deployment(self, dep: Deployment) -> None:
        try:
            await self._execute(
                """INSERT INTO deployments
                   (id, agent_name, environment, status, rules, experiment, previous_rules, created_by, created_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                   ON CONFLICT(agent_name, environment) DO UPDATE SET
                     status=excluded.status, rules=excluded.rules, experiment=excluded.experiment,
                     previous_rules=excluded.previous_rules, created_by=excluded.created_by,
                     updated_at=excluded.updated_at""",
                dep.id, dep.agent_name, dep.environment, dep.status,
                [r.to_dict() for r in dep.rules],
                dep.experiment,
                [r.to_dict() for r in dep.previous_rules] if dep.previous_rules else None,
                dep.created_by, to_db(dep.created_at), to_db(dep.updated_at),
            )
        except Exception as e:
            raise ConflictError(
                f"deployment ({dep.agent_name}, {dep.environment}) 唯一键冲突"
            ) from e

    async def get_deployment(self, agent: str, environment: str) -> Deployment | None:
        row = await self._fetchrow(
            "SELECT * FROM deployments WHERE agent_name=$1 AND environment=$2", agent, environment
        )
        return self._row_to_deployment(row) if row else None

    async def get_deployment_by_id(self, id: str) -> Deployment | None:
        row = await self._fetchrow("SELECT * FROM deployments WHERE id=$1", id)
        return self._row_to_deployment(row) if row else None

    async def list_deployments(self) -> list[Deployment]:
        rows = await self._fetch("SELECT * FROM deployments ORDER BY updated_at DESC")
        return [self._row_to_deployment(r) for r in rows]

    @staticmethod
    def _row_to_deployment(row: Any) -> Deployment:
        from ..domain.deployment import DeploymentRule

        return Deployment(
            id=str(row["id"]),
            agent_name=row["agent_name"],
            environment=row["environment"],
            status=row["status"],
            rules=[DeploymentRule.from_dict(r) for r in loads(row["rules"]) or []],
            experiment=row["experiment"],
            previous_rules=[DeploymentRule.from_dict(r) for r in loads(row["previous_rules"]) or []]
            if row["previous_rules"] is not None
            else None,
            created_by=row["created_by"] or "",
            created_at=from_db(row["created_at"]),
            updated_at=from_db(row["updated_at"]),
        )

    # ---- audit_logs -------------------------------------------------------
    async def append_audit(self, entry: AuditEntry) -> AuditEntry:
        row = await self._fetchrow(
            "INSERT INTO audit_logs (actor, action, resource_type, resource_id, before, after, reason, created_at)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
            entry.actor, entry.action, entry.resource_type, entry.resource_id,
            entry.before, entry.after, entry.reason, to_db(entry.created_at),
        )
        entry.id = row["id"]
        return entry

    async def list_audit(
        self, *, limit: int = 100, action: str | None = None, agent: str | None = None
    ) -> list[AuditEntry]:
        sql = "SELECT * FROM audit_logs WHERE 1=1"
        params: list[Any] = []
        if action:
            sql += " AND action=$" + str(len(params) + 1)
            params.append(action)
        if agent:
            sql += " AND resource_id LIKE $" + str(len(params) + 1)
            params.append(f"%{agent}%")
        sql += " ORDER BY created_at DESC, id DESC LIMIT $" + str(len(params) + 1)
        params.append(limit)
        rows = await self._fetch(sql, *params)
        return [self._row_to_audit(r) for r in rows]

    @staticmethod
    def _row_to_audit(row: Any) -> AuditEntry:
        return AuditEntry(
            id=row["id"],
            actor=row["actor"] or "",
            action=row["action"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            before=loads(row["before"]),
            after=loads(row["after"]),
            reason=row["reason"] or "",
            created_at=from_db(row["created_at"]),
        )

    async def close(self) -> None:
        await self.db.close()
