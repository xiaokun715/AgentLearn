"""SQLite 存储后端 —— 默认后端：零依赖、重启不丢（设计说明书 §34-35 表结构）。

建表语句与 §34 executions / audit_events、§35 sandbox_policies 对齐。
resource_usage 以 JSON 文本存储，读取时反序列化。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from ..domain.execution import Execution, ExecutionStatus
from ..domain.policy import Policy
from .execution_store import ExecutionStore
from .policy_store import PolicyStore, default_policy_objects

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    id            TEXT PRIMARY KEY,
    tenant_id     VARCHAR(128) NOT NULL,
    user_id       VARCHAR(128) NOT NULL DEFAULT 'anonymous',
    agent_id      VARCHAR(128) NOT NULL DEFAULT 'anonymous',
    tool_type     VARCHAR(64)  NOT NULL,
    status        VARCHAR(32)  NOT NULL,
    policy_id     VARCHAR(128) NOT NULL,
    runtime_id    VARCHAR(256),
    container_id  VARCHAR(256),
    exit_code     INTEGER,
    stdout        TEXT,
    stderr        TEXT,
    error         TEXT,
    resource_usage TEXT,
    created_at    TIMESTAMP NOT NULL,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    duration_ms   INTEGER
);

CREATE TABLE IF NOT EXISTS sandbox_policies (
    name       VARCHAR(128) PRIMARY KEY,
    version    INTEGER NOT NULL,
    config     TEXT    NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    event_type   VARCHAR(64) NOT NULL,
    payload      TEXT,
    created_at   TIMESTAMP NOT NULL
);
"""


class SqliteDatabase:
    def __init__(self, url: str) -> None:
        path = url.replace("sqlite:///", "", 1)
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        import os
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._seed_policies()
        await self._conn.commit()
        logger.info("sqlite ready: %s", self.path)

    async def _seed_policies(self) -> None:
        for policy in default_policy_objects():
            await self._conn.execute(
                "INSERT OR IGNORE INTO sandbox_policies (name, version, config, created_at)"
                " VALUES (?, ?, ?, ?)",
                (policy.name, policy.version, policy.to_json(), _ts(policy.created_at)),
            )

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self._conn.execute(sql, params)
        await self._conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> Any:
        cur = await self._conn.execute(sql, params)
        return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        cur = await self._conn.execute(sql, params)
        return await cur.fetchall()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _row_to_execution(row: aiosqlite.Row) -> Execution:
    return Execution(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"] or "anonymous",
        agent_id=row["agent_id"] or "anonymous",
        tool_type=row["tool_type"],
        code="",  # 代码不落库（只存审计需要的元数据；生产可按需加密存储）
        status=ExecutionStatus.from_str(row["status"]),
        policy_id=row["policy_id"],
        runtime_id=row["runtime_id"],
        container_id=row["container_id"],
        exit_code=row["exit_code"],
        stdout=row["stdout"] or "",
        stderr=row["stderr"] or "",
        error=row["error"],
        created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
        started_at=_dt(row["started_at"]),
        finished_at=_dt(row["finished_at"]),
        duration_ms=row["duration_ms"],
        resource_usage=json.loads(row["resource_usage"]) if row["resource_usage"] else {},
    )


class SqliteExecutionStore(ExecutionStore):
    def __init__(self, db: SqliteDatabase) -> None:
        self.db = db

    async def save(self, execution: Execution) -> None:
        await self.db.execute(
            """
            INSERT INTO executions (
                id, tenant_id, user_id, agent_id, tool_type, status, policy_id,
                runtime_id, container_id, exit_code, stdout, stderr, error,
                resource_usage, created_at, started_at, finished_at, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                runtime_id=excluded.runtime_id,
                container_id=excluded.container_id,
                exit_code=excluded.exit_code,
                stdout=excluded.stdout,
                stderr=excluded.stderr,
                error=excluded.error,
                resource_usage=excluded.resource_usage,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                duration_ms=excluded.duration_ms
            """,
            (
                execution.id, execution.tenant_id, execution.user_id, execution.agent_id,
                execution.tool_type, execution.status.value, execution.policy_id,
                execution.runtime_id, execution.container_id, execution.exit_code,
                execution.stdout, execution.stderr, execution.error,
                json.dumps(execution.resource_usage, ensure_ascii=False),
                _ts(execution.created_at), _ts(execution.started_at),
                _ts(execution.finished_at), execution.duration_ms,
            ),
        )

    async def get(self, execution_id: str) -> Execution | None:
        row = await self.db.fetchone(
            "SELECT * FROM executions WHERE id = ?", (execution_id,)
        )
        return _row_to_execution(row) if row else None

    async def list(self, *, limit: int = 50, tenant_id: str | None = None) -> list[Execution]:
        if tenant_id:
            rows = await self.db.fetchall(
                "SELECT * FROM executions WHERE tenant_id = ?"
                " ORDER BY created_at DESC LIMIT ?", (tenant_id, limit)
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM executions ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [_row_to_execution(r) for r in rows]


class SqlitePolicyStore(PolicyStore):
    def __init__(self, db: SqliteDatabase) -> None:
        self.db = db

    async def save(self, policy: Policy) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO sandbox_policies (name, version, config, created_at)"
            " VALUES (?, ?, ?, ?)",
            (policy.name, policy.version, policy.to_json(), _ts(policy.created_at)),
        )

    async def get(self, name: str) -> Policy | None:
        row = await self.db.fetchone(
            "SELECT config FROM sandbox_policies WHERE name = ? ORDER BY version DESC LIMIT 1",
            (name,),
        )
        if not row:
            return None
        data = json.loads(row["config"])
        return Policy.from_dict(data)

    async def get_default(self) -> Policy | None:
        return await self.get("python_basic")

    async def list(self) -> list[Policy]:
        rows = await self.db.fetchall(
            "SELECT config FROM sandbox_policies ORDER BY name, version DESC"
        )
        seen: set[str] = set()
        policies: list[Policy] = []
        for row in rows:
            data = json.loads(row["config"])
            if data["name"] not in seen:
                seen.add(data["name"])
                policies.append(Policy.from_dict(data))
        return policies


class SqliteAuditStore:
    """审计事件落库（§34 audit_events）。AuditLogger 在内存里先缓存，这里负责持久化。"""

    def __init__(self, db: SqliteDatabase) -> None:
        self.db = db

    async def append(self, *, execution_id: str, event_type: str, payload: dict) -> None:
        await self.db.execute(
            "INSERT INTO audit_events (execution_id, event_type, payload, created_at)"
            " VALUES (?, ?, ?, ?)",
            (execution_id, event_type, json.dumps(payload, ensure_ascii=False, default=str),
             datetime.now(timezone.utc).isoformat()),
        )
