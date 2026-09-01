"""PostgreSQL 存储后端（设计说明书 §34-35 推荐栈）—— 生产路径。

依赖可选：pip install agent-tool-sandbox[postgres]（asyncpg）。
用法：STORAGE_BACKEND=postgres DATABASE_URL=postgresql://sandbox:sandbox@localhost:5432/sandbox
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ..domain.execution import Execution, ExecutionStatus
from ..domain.policy import Policy
from .execution_store import ExecutionStore
from .policy_store import PolicyStore, default_policy_objects

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    id             UUID PRIMARY KEY,
    tenant_id      VARCHAR(128) NOT NULL,
    user_id        VARCHAR(128) NOT NULL DEFAULT 'anonymous',
    agent_id       VARCHAR(128) NOT NULL DEFAULT 'anonymous',
    tool_type      VARCHAR(64)  NOT NULL,
    status         VARCHAR(32)  NOT NULL,
    policy_id      VARCHAR(128) NOT NULL,
    runtime_id     VARCHAR(256),
    container_id   VARCHAR(256),
    exit_code      INTEGER,
    stdout         TEXT,
    stderr         TEXT,
    error          TEXT,
    resource_usage JSONB,
    created_at     TIMESTAMPTZ NOT NULL,
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    duration_ms    INTEGER
);

CREATE TABLE IF NOT EXISTS sandbox_policies (
    name       VARCHAR(128) PRIMARY KEY,
    version    INTEGER NOT NULL,
    config     JSONB    NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id           BIGSERIAL PRIMARY KEY,
    execution_id UUID NOT NULL,
    event_type   VARCHAR(64) NOT NULL,
    payload      JSONB,
    created_at   TIMESTAMPTZ NOT NULL
);
"""


class PostgresDatabase:
    def __init__(self, url: str) -> None:
        self.url = url
        self._pool: Any | None = None

    async def connect(self) -> None:
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "STORAGE_BACKEND=postgres 需要 pip install agent-tool-sandbox[postgres]"
            ) from exc
        self._pool = await asyncpg.create_pool(self.url)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)
            for policy in default_policy_objects():
                await conn.execute(
                    "INSERT INTO sandbox_policies (name, version, config, created_at)"
                    " VALUES ($1, $2, $3, $4) ON CONFLICT (name) DO NOTHING",
                    policy.name, policy.version, policy.to_dict(), policy.created_at,
                )
        logger.info("postgres ready: %s", self.url.split("@")[-1])

    async def execute(self, sql: str, params: tuple = ()) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *params)

    async def fetchone(self, sql: str, params: tuple = ()) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *params)

    async def fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(sql, *params)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def _row_to_execution(row: Any) -> Execution:
    return Execution(
        id=str(row["id"]),
        tenant_id=row["tenant_id"],
        user_id=row["user_id"] or "anonymous",
        agent_id=row["agent_id"] or "anonymous",
        tool_type=row["tool_type"],
        code="",
        status=ExecutionStatus.from_str(row["status"]),
        policy_id=row["policy_id"],
        runtime_id=row["runtime_id"],
        container_id=row["container_id"],
        exit_code=row["exit_code"],
        stdout=row["stdout"] or "",
        stderr=row["stderr"] or "",
        error=row["error"],
        created_at=row["created_at"] or datetime.now(timezone.utc),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        resource_usage=dict(row["resource_usage"]) if row["resource_usage"] else {},
    )


class PostgresExecutionStore(ExecutionStore):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    async def save(self, execution: Execution) -> None:
        await self.db.execute(
            """
            INSERT INTO executions (
                id, tenant_id, user_id, agent_id, tool_type, status, policy_id,
                runtime_id, container_id, exit_code, stdout, stderr, error,
                resource_usage, created_at, started_at, finished_at, duration_ms
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
            ON CONFLICT (id) DO UPDATE SET
                status=EXCLUDED.status, runtime_id=EXCLUDED.runtime_id,
                container_id=EXCLUDED.container_id, exit_code=EXCLUDED.exit_code,
                stdout=EXCLUDED.stdout, stderr=EXCLUDED.stderr, error=EXCLUDED.error,
                resource_usage=EXCLUDED.resource_usage, started_at=EXCLUDED.started_at,
                finished_at=EXCLUDED.finished_at, duration_ms=EXCLUDED.duration_ms
            """,
            (
                execution.id, execution.tenant_id, execution.user_id, execution.agent_id,
                execution.tool_type, execution.status.value, execution.policy_id,
                execution.runtime_id, execution.container_id, execution.exit_code,
                execution.stdout, execution.stderr, execution.error,
                execution.resource_usage, execution.created_at,
                execution.started_at, execution.finished_at, execution.duration_ms,
            ),
        )

    async def get(self, execution_id: str) -> Execution | None:
        row = await self.db.fetchone(
            "SELECT * FROM executions WHERE id = $1", (execution_id,)
        )
        return _row_to_execution(row) if row else None

    async def list(self, *, limit: int = 50, tenant_id: str | None = None) -> list[Execution]:
        if tenant_id:
            rows = await self.db.fetchall(
                "SELECT * FROM executions WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT $2",
                (tenant_id, limit),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM executions ORDER BY created_at DESC LIMIT $1", (limit,)
            )
        return [_row_to_execution(r) for r in rows]


class PostgresPolicyStore(PolicyStore):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    async def save(self, policy: Policy) -> None:
        await self.db.execute(
            "INSERT INTO sandbox_policies (name, version, config, created_at)"
            " VALUES ($1, $2, $3, $4) ON CONFLICT (name) DO UPDATE SET"
            " version=EXCLUDED.version, config=EXCLUDED.config",
            (policy.name, policy.version, policy.to_dict(), policy.created_at),
        )

    async def get(self, name: str) -> Policy | None:
        row = await self.db.fetchone(
            "SELECT config FROM sandbox_policies WHERE name = $1 ORDER BY version DESC LIMIT 1",
            (name,),
        )
        if not row:
            return None
        return Policy.from_dict(dict(row["config"]))

    async def get_default(self) -> Policy | None:
        return await self.get("python_basic")

    async def list(self) -> list[Policy]:
        rows = await self.db.fetchall(
            "SELECT config FROM sandbox_policies ORDER BY name, version DESC"
        )
        seen: set[str] = set()
        policies: list[Policy] = []
        for row in rows:
            data = dict(row["config"])
            if data["name"] not in seen:
                seen.add(data["name"])
                policies.append(Policy.from_dict(data))
        return policies


class PostgresAuditStore:
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    async def append(self, *, execution_id: str, event_type: str, payload: dict) -> None:
        await self.db.execute(
            "INSERT INTO audit_events (execution_id, event_type, payload, created_at)"
            " VALUES ($1, $2, $3, $4)",
            (execution_id, event_type, payload, datetime.now(timezone.utc)),
        )
