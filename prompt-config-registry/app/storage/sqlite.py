"""SQLite 版 Repository —— 默认持久化后端（零外部依赖）。

文件即库（默认 ``data/registry.db``），DDL 与 migrations/ 下的 PostgreSQL 方言同构。
启动时 ``CREATE TABLE IF NOT EXISTS``，无需额外迁移步骤。
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import aiosqlite

from ..domain.audit import AuditEntry
from ..domain.config import AgentConfig
from ..domain.deployment import Deployment
from ..domain.exceptions import ConflictError
from ..domain.prompt import Prompt, PromptVersion
from .models import dumps, from_db, loads, to_db
from .repository import RegistryRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
    id         TEXT PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id         TEXT PRIMARY KEY,
    prompt_id  TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL,
    template   TEXT NOT NULL,
    variables  TEXT,
    metadata   TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (prompt_id, version)
);

CREATE TABLE IF NOT EXISTS agent_configs (
    id         TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    version    INTEGER NOT NULL,
    config     TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (agent_name, version)
);

CREATE TABLE IF NOT EXISTS deployments (
    id             TEXT PRIMARY KEY,
    agent_name     TEXT NOT NULL,
    environment    TEXT NOT NULL,
    status         TEXT NOT NULL,
    rules          TEXT NOT NULL,
    experiment     TEXT,
    previous_rules TEXT,
    created_by     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (agent_name, environment)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    actor         TEXT,
    action        TEXT,
    resource_type TEXT,
    resource_id   TEXT,
    before        TEXT,
    after         TEXT,
    reason        TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prompt_versions_prompt ON prompt_versions (prompt_id, version);
CREATE INDEX IF NOT EXISTS idx_agent_configs_agent   ON agent_configs (agent_name, version);
CREATE INDEX IF NOT EXISTS idx_audit_created         ON audit_logs (created_at DESC);
"""


class SqliteDatabase:
    """共享单连接 + WAL，封装常用异步查询。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        # _SCHEMA 含多条建表语句，必须用 executescript
        await conn.executescript(_SCHEMA)
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteDatabase 未 connect()")
        return self._conn

    async def execute(self, sql: str, *params: Any) -> None:
        # 游标式写法：async with conn.execute(...) as cur 即执行 SQL
        # （aiosqlite 的 Connection 是后台线程，不能用 async with conn 重复启动）
        async with self._require().execute(sql, params) as cur:
            pass
        await self._require().commit()

    async def fetchone(self, sql: str, *params: Any) -> aiosqlite.Row | None:
        async with self._require().execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, *params: Any) -> list[aiosqlite.Row]:
        async with self._require().execute(sql, params) as cur:
            return await cur.fetchall()


class SqliteRepository(RegistryRepository):
    def __init__(self, db: SqliteDatabase) -> None:
        self.db = db

    # ---- prompts ----------------------------------------------------------
    async def create_prompt(self, prompt: Prompt) -> None:
        try:
            await self.db.execute(
                "INSERT INTO prompts (id, name, created_by, created_at) VALUES (?,?,?,?)",
                prompt.id, prompt.name, prompt.created_by, to_db(prompt.created_at),
            )
        except aiosqlite.IntegrityError as e:
            raise ConflictError(f"Prompt '{prompt.name}' 已存在") from e

    async def get_prompt(self, name: str) -> Prompt | None:
        row = await self.db.fetchone("SELECT * FROM prompts WHERE name=?", name)
        return self._row_to_prompt(row) if row else None

    async def list_prompts(self) -> list[Prompt]:
        rows = await self.db.fetchall("SELECT * FROM prompts ORDER BY created_at")
        return [self._row_to_prompt(r) for r in rows]

    @staticmethod
    def _row_to_prompt(row: aiosqlite.Row) -> Prompt:
        return Prompt(
            id=row["id"],
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
            await self.db.execute(
                "INSERT INTO prompt_versions (id, prompt_id, version, template, variables, metadata, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                pv.id, prompt.id, pv.version, pv.template,
                dumps(pv.variables), dumps(pv.metadata),
                pv.created_by, to_db(pv.created_at),
            )
        except aiosqlite.IntegrityError as e:
            raise ConflictError(
                f"Prompt {pv.prompt_name} 的版本 v{pv.version} 已存在（不可变，只能追加新版本）"
            ) from e

    async def get_prompt_version(self, name: str, version: int) -> PromptVersion | None:
        row = await self.db.fetchone(
            """SELECT pv.*, p.name AS prompt_name FROM prompt_versions pv
               JOIN prompts p ON p.id = pv.prompt_id
               WHERE p.name=? AND pv.version=?""",
            name, version,
        )
        return self._row_to_pv(row) if row else None

    async def get_prompt_version_by_id(self, id: str) -> PromptVersion | None:
        row = await self.db.fetchone(
            """SELECT pv.*, p.name AS prompt_name FROM prompt_versions pv
               JOIN prompts p ON p.id = pv.prompt_id WHERE pv.id=?""",
            id,
        )
        return self._row_to_pv(row) if row else None

    async def list_prompt_versions(self, name: str) -> list[PromptVersion]:
        rows = await self.db.fetchall(
            """SELECT pv.*, p.name AS prompt_name FROM prompt_versions pv
               JOIN prompts p ON p.id = pv.prompt_id
               WHERE p.name=? ORDER BY pv.version""",
            name,
        )
        return [self._row_to_pv(r) for r in rows]

    async def next_prompt_version(self, name: str) -> int:
        row = await self.db.fetchone(
            """SELECT COALESCE(MAX(pv.version), 0) + 1 AS n FROM prompt_versions pv
               JOIN prompts p ON p.id = pv.prompt_id WHERE p.name=?""",
            name,
        )
        return int(row["n"])

    @staticmethod
    def _row_to_pv(row: aiosqlite.Row) -> PromptVersion:
        return PromptVersion(
            id=row["id"],
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
            await self.db.execute(
                "INSERT INTO agent_configs (id, agent_name, version, config, created_by, created_at)"
                " VALUES (?,?,?,?,?,?)",
                str(uuid.uuid4()), config.agent_name, config.version,
                dumps(config.to_dict()), config.created_by, to_db(config.created_at),
            )
        except aiosqlite.IntegrityError as e:
            raise ConflictError(
                f"Config {config.agent_name} 的版本 v{config.version} 已存在（不可变，只能追加新版本）"
            ) from e

    async def get_config(self, agent: str, version: int) -> AgentConfig | None:
        row = await self.db.fetchone(
            "SELECT * FROM agent_configs WHERE agent_name=? AND version=?", agent, version
        )
        return self._row_to_config(row) if row else None

    async def list_configs(self, agent: str) -> list[AgentConfig]:
        rows = await self.db.fetchall(
            "SELECT * FROM agent_configs WHERE agent_name=? ORDER BY version", agent
        )
        return [self._row_to_config(r) for r in rows]

    async def next_config_version(self, agent: str) -> int:
        row = await self.db.fetchone(
            "SELECT COALESCE(MAX(version), 0) + 1 AS n FROM agent_configs WHERE agent_name=?",
            agent,
        )
        return int(row["n"])

    @staticmethod
    def _row_to_config(row: aiosqlite.Row) -> AgentConfig:
        data = loads(row["config"])
        data["agent"] = data.get("agent", row["agent_name"])
        data["version"] = data.get("version", row["version"])
        data["created_by"] = data.get("created_by", row["created_by"])
        data["created_at"] = data.get("created_at", row["created_at"])
        return AgentConfig.from_dict(data)

    # ---- deployments ------------------------------------------------------
    async def upsert_deployment(self, dep: Deployment) -> None:
        try:
            await self.db.execute(
                """INSERT INTO deployments
                   (id, agent_name, environment, status, rules, experiment, previous_rules, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(agent_name, environment) DO UPDATE SET
                     status=excluded.status, rules=excluded.rules, experiment=excluded.experiment,
                     previous_rules=excluded.previous_rules, created_by=excluded.created_by,
                     updated_at=excluded.updated_at""",
                dep.id, dep.agent_name, dep.environment, dep.status,
                dumps([r.to_dict() for r in dep.rules]),
                dep.experiment,
                dumps([r.to_dict() for r in dep.previous_rules]) if dep.previous_rules else None,
                dep.created_by, to_db(dep.created_at), to_db(dep.updated_at),
            )
        except aiosqlite.IntegrityError as e:
            raise ConflictError(
                f"deployment ({dep.agent_name}, {dep.environment}) 唯一键冲突"
            ) from e

    async def get_deployment(self, agent: str, environment: str) -> Deployment | None:
        row = await self.db.fetchone(
            "SELECT * FROM deployments WHERE agent_name=? AND environment=?",
            agent, environment,
        )
        return self._row_to_deployment(row) if row else None

    async def get_deployment_by_id(self, id: str) -> Deployment | None:
        row = await self.db.fetchone("SELECT * FROM deployments WHERE id=?", id)
        return self._row_to_deployment(row) if row else None

    async def list_deployments(self) -> list[Deployment]:
        rows = await self.db.fetchall("SELECT * FROM deployments ORDER BY updated_at DESC")
        return [self._row_to_deployment(r) for r in rows]

    @staticmethod
    def _row_to_deployment(row: aiosqlite.Row) -> Deployment:
        return Deployment(
            id=row["id"],
            agent_name=row["agent_name"],
            environment=row["environment"],
            status=row["status"],
            rules=_rules(row["rules"]),
            experiment=row["experiment"],
            previous_rules=_rules(row["previous_rules"]) if row["previous_rules"] else None,
            created_by=row["created_by"] or "",
            created_at=from_db(row["created_at"]),
            updated_at=from_db(row["updated_at"]),
        )

    # ---- audit_logs -------------------------------------------------------
    async def append_audit(self, entry: AuditEntry) -> AuditEntry:
        row = await self.db.fetchone(
            "INSERT INTO audit_logs (actor, action, resource_type, resource_id, before, after, reason, created_at)"
            " VALUES (?,?,?,?,?,?,?,?) RETURNING id",
            entry.actor, entry.action, entry.resource_type, entry.resource_id,
            dumps(entry.before) if entry.before is not None else None,
            dumps(entry.after) if entry.after is not None else None,
            entry.reason, to_db(entry.created_at),
        )
        entry.id = row["id"]
        return entry

    async def list_audit(
        self, *, limit: int = 100, action: str | None = None, agent: str | None = None
    ) -> list[AuditEntry]:
        sql = "SELECT * FROM audit_logs WHERE 1=1"
        params: list[Any] = []
        if action:
            sql += " AND action=?"
            params.append(action)
        if agent:
            sql += " AND resource_id LIKE ?"
            params.append(f"%{agent}%")
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = await self.db.fetchall(sql, *params)
        return [self._row_to_audit(r) for r in rows]

    @staticmethod
    def _row_to_audit(row: aiosqlite.Row) -> AuditEntry:
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


def _rules(raw: str) -> list:
    """反序列化 rules JSONB（text 存储）。"""
    from ..domain.deployment import DeploymentRule

    return [DeploymentRule.from_dict(r) for r in loads(raw)]
