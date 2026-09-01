"""SQLite 版 JobStore / EventStore —— 默认持久化后端。

不需要任何外部服务，文件即库（默认 ``data/jobs.db``），
足以支撑「进程崩溃 -> 重启 -> Reaper 恢复 Job」的完整故障演练。
DDL 与 migrations/ 下的 PostgreSQL 方言同构。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import aiosqlite

from ..domain.events import JobEvent
from ..domain.job import Job
from ..domain.status import JobStatus
from .event_store import EventStore
from .job_store import JobStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    agent_name       TEXT NOT NULL,
    input            TEXT NOT NULL,
    status           TEXT NOT NULL,
    priority         INTEGER NOT NULL DEFAULT 0,
    retry_count      INTEGER NOT NULL DEFAULT 0,
    max_retries      INTEGER NOT NULL DEFAULT 3,
    current_step     TEXT,
    progress         INTEGER NOT NULL DEFAULT 0,
    worker_id        TEXT,
    lease_expire_at  REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    queued_at        REAL,
    started_at       REAL,
    finished_at      REAL,
    result           TEXT,
    error            TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_lease     ON jobs (status, lease_expire_at);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant    ON jobs (tenant_id, status);

CREATE TABLE IF NOT EXISTS checkpoints (
    job_id     TEXT PRIMARY KEY,
    step       TEXT NOT NULL,
    state      TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS job_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload    TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events (job_id, id);
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
        self._conn = await aiosqlite.connect(self.path, check_same_thread=False)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()
        await self._conn.executescript(_SCHEMA)  # 多语句 DDL 用 executescript
        await self.commit()

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        assert self._conn is not None, "SqliteDatabase.connect() must be called first"
        return await self._conn.execute(sql, params)

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        cur = await self.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = await self.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def commit(self) -> None:
        assert self._conn is not None
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def connection(self) -> aiosqlite.Connection:
        assert self._conn is not None
        return self._conn


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _loads(raw: str | None) -> Any:
    if raw is None:
        return None
    return json.loads(raw)


class SqliteJobStore(JobStore):
    def __init__(self, db: SqliteDatabase) -> None:
        self.db = db

    async def create(self, job: Job) -> Job:
        await self.db.execute(
            """
            INSERT INTO jobs (
                id, tenant_id, agent_name, input, status, priority, retry_count,
                max_retries, current_step, progress, worker_id, lease_expire_at,
                cancel_requested, queued_at, started_at, finished_at, result, error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id, job.tenant_id, job.agent_name, _dumps(job.input), job.status.value,
                job.priority, job.retry_count, job.max_retries, job.current_step, job.progress,
                job.worker_id, job.lease_expire_at, int(job.cancel_requested),
                job.queued_at, job.started_at, job.finished_at,
                _dumps(job.result) if job.result is not None else None,
                job.error, job.created_at, job.updated_at,
            ),
        )
        await self.db.commit()
        return job

    async def get(self, job_id: str) -> Job | None:
        row = await self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._row_to_job(row) if row else None

    async def update(self, job: Job) -> Job:
        await self.db.execute(
            """
            UPDATE jobs SET tenant_id=?, agent_name=?, input=?, status=?, priority=?,
                retry_count=?, max_retries=?, current_step=?, progress=?, worker_id=?,
                lease_expire_at=?, cancel_requested=?, queued_at=?, started_at=?,
                finished_at=?, result=?, error=?, updated_at=?
            WHERE id=?
            """,
            (
                job.tenant_id, job.agent_name, _dumps(job.input), job.status.value, job.priority,
                job.retry_count, job.max_retries, job.current_step, job.progress, job.worker_id,
                job.lease_expire_at, int(job.cancel_requested), job.queued_at, job.started_at,
                job.finished_at, _dumps(job.result) if job.result is not None else None,
                job.error, job.updated_at, job.id,
            ),
        )
        await self.db.commit()
        return job

    async def transition(
        self, job_id: str, from_status: JobStatus, to_status: JobStatus, **fields: Any
    ) -> bool:
        # 条件更新：WHERE status = from_status，rowcount==1 才成功
        sets = ["status=?", "updated_at=?"]
        params: list[Any] = [to_status.value, time.time()]
        if "current_step" in fields:
            sets.append("current_step=?")
            params.append(fields["current_step"])
        if "progress" in fields:
            sets.append("progress=?")
            params.append(fields["progress"])
        if "worker_id" in fields:
            sets.append("worker_id=?")
            params.append(fields["worker_id"])
        if "lease_expire_at" in fields:
            sets.append("lease_expire_at=?")
            params.append(fields["lease_expire_at"])
        if "error" in fields:
            sets.append("error=?")
            params.append(fields["error"])
        if "result" in fields:
            sets.append("result=?")
            params.append(_dumps(fields["result"]) if fields["result"] is not None else None)
        if "retry_count" in fields:
            sets.append("retry_count=?")
            params.append(fields["retry_count"])
        if "started_at" in fields:
            sets.append("started_at=?")
            params.append(fields["started_at"])
        if "finished_at" in fields:
            sets.append("finished_at=?")
            params.append(fields["finished_at"])
        if "cancel_requested" in fields:
            sets.append("cancel_requested=?")
            params.append(int(fields["cancel_requested"]))
        params.extend([job_id, from_status.value])
        cur = await self.db.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE id=? AND status=?",
            tuple(params),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def acquire_lease(self, job_id: str, worker_id: str, duration: float) -> bool:
        now = time.time()
        # 可被获取：状态可执行（queued/retrying），或租约已过期可接管，或自己续约
        cur = await self.db.execute(
            """
            UPDATE jobs SET worker_id=?, lease_expire_at=?, updated_at=?
            WHERE id=?
              AND (
                    status IN ('queued', 'retrying')
                 OR lease_expire_at IS NULL
                 OR lease_expire_at < ?
                 OR worker_id = ?
              )
            """,
            (worker_id, now + duration, now, job_id, now, worker_id),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def renew_lease(self, job_id: str, worker_id: str, duration: float) -> bool:
        now = time.time()
        cur = await self.db.execute(
            "UPDATE jobs SET lease_expire_at=?, updated_at=? WHERE id=? AND worker_id=?",
            (now + duration, now, job_id, worker_id),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def release_lease(self, job_id: str, worker_id: str) -> bool:
        cur = await self.db.execute(
            "UPDATE jobs SET worker_id=NULL, lease_expire_at=NULL, updated_at=? WHERE id=? AND worker_id=?",
            (time.time(), job_id, worker_id),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def expire_lease(self, job_id: str, worker_id: str) -> bool:
        cur = await self.db.execute(
            "UPDATE jobs SET lease_expire_at=?, updated_at=? WHERE id=? AND worker_id=?",
            (time.time() - 1.0, time.time(), job_id, worker_id),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def update_progress(
        self, job_id: str, worker_id: str, *, step: str | None, progress: int
    ) -> bool:
        if step is not None:
            cur = await self.db.execute(
                "UPDATE jobs SET current_step=?, progress=?, updated_at=? WHERE id=? AND worker_id=?",
                (step, progress, time.time(), job_id, worker_id),
            )
        else:
            cur = await self.db.execute(
                "UPDATE jobs SET progress=?, updated_at=? WHERE id=? AND worker_id=?",
                (progress, time.time(), job_id, worker_id),
            )
        await self.db.commit()
        return cur.rowcount == 1

    async def find_recoverable(self, now: float, grace: float) -> list[Job]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM jobs
            WHERE status IN ('running', 'retrying')
              AND lease_expire_at IS NOT NULL
              AND lease_expire_at < ?
            """,
            (now - grace,),
        )
        return [self._row_to_job(r) for r in rows]

    async def set_cancel_requested(self, job_id: str, value: bool = True) -> bool:
        cur = await self.db.execute(
            "UPDATE jobs SET cancel_requested=?, updated_at=? WHERE id=?",
            (int(value), time.time(), job_id),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def is_cancel_requested(self, job_id: str) -> bool:
        row = await self.db.fetchone(
            "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
        )
        return bool(row and row["cancel_requested"])

    async def list_dead(self) -> list[Job]:
        rows = await self.db.fetchall("SELECT * FROM jobs WHERE status = 'dead' ORDER BY updated_at")
        return [self._row_to_job(r) for r in rows]

    async def count_by_status(self) -> dict[str, int]:
        rows = await self.db.fetchall("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    async def close(self) -> None:
        return None

    @staticmethod
    def _row_to_job(row: dict) -> Job:
        return Job.from_dict(
            {
                "id": row["id"],
                "tenant_id": row["tenant_id"],
                "agent_name": row["agent_name"],
                "input": _loads(row["input"]),
                "status": row["status"],
                "priority": row["priority"],
                "retry_count": row["retry_count"],
                "max_retries": row["max_retries"],
                "current_step": row["current_step"],
                "progress": row["progress"],
                "worker_id": row["worker_id"],
                "lease_expire_at": row["lease_expire_at"],
                "cancel_requested": bool(row["cancel_requested"]),
                "queued_at": row["queued_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "result": _loads(row["result"]),
                "error": row["error"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )


class SqliteEventStore(EventStore):
    def __init__(self, db: SqliteDatabase) -> None:
        self.db = db

    async def append(self, job_id: str, event_type: str, payload: dict | None = None) -> JobEvent:
        now = time.time()
        cur = await self.db.execute(
            "INSERT INTO job_events (job_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
            (job_id, event_type, _dumps(payload) if payload is not None else None, now),
        )
        await self.db.commit()
        ev = JobEvent(job_id=job_id, event_type=event_type, payload=payload, created_at=now)
        ev.seq = cur.lastrowid
        return ev

    async def list(self, job_id: str) -> list[JobEvent]:
        rows = await self.db.fetchall(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)
        )
        return [
            JobEvent(
                job_id=r["job_id"],
                event_type=r["event_type"],
                payload=_loads(r["payload"]),
                seq=r["id"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def close(self) -> None:
        return None
