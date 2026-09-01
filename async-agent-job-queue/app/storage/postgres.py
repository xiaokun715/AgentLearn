"""PostgreSQL 版 JobStore / EventStore（可选后端，设计说明书 §32-34）。

需要 ``pip install asyncpg`` 与运行中的 PostgreSQL（见 docker-compose.yml）。
未安装 asyncpg 或未配置 DATABASE_URL 时不会被加载。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from ..domain.events import JobEvent
from ..domain.job import Job
from ..domain.status import JobStatus
from .event_store import EventStore
from .job_store import JobStore


def _epoch(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _ts(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=timezone.utc) if value is not None else None


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _loads(raw: Any) -> Any:
    return json.loads(raw) if raw is not None else None


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


class PostgresJobStore(JobStore):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    async def _run(self, sql: str, *args: Any) -> Any:
        async with self.db._acq as conn:
            return await conn.fetchrow(sql, *args)

    async def _fetch(self, sql: str, *args: Any) -> list[Any]:
        async with self.db._acq as conn:
            return await conn.fetch(sql, *args)

    async def create(self, job: Job) -> Job:
        await self._run(
            """
            INSERT INTO jobs (
                id, tenant_id, agent_name, input, status, priority, retry_count,
                max_retries, current_step, progress, worker_id, lease_expire_at,
                cancel_requested, queued_at, started_at, finished_at, result, error,
                created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
            """,
            job.id, job.tenant_id, job.agent_name, _dumps(job.input), job.status.value,
            job.priority, job.retry_count, job.max_retries, job.current_step, job.progress,
            job.worker_id, _ts(job.lease_expire_at), job.cancel_requested, _ts(job.queued_at),
            _ts(job.started_at), _ts(job.finished_at),
            _dumps(job.result) if job.result is not None else None, job.error,
            _ts(job.created_at), _ts(job.updated_at),
        )
        return job

    async def get(self, job_id: str) -> Job | None:
        row = await self._run("SELECT * FROM jobs WHERE id=$1", job_id)
        return self._row_to_job(row) if row else None

    async def update(self, job: Job) -> Job:
        await self._run(
            """
            UPDATE jobs SET tenant_id=$2, agent_name=$3, input=$4, status=$5, priority=$6,
                retry_count=$7, max_retries=$8, current_step=$9, progress=$10, worker_id=$11,
                lease_expire_at=$12, cancel_requested=$13, queued_at=$14, started_at=$15,
                finished_at=$16, result=$17, error=$18, updated_at=$19
            WHERE id=$1
            """,
            job.id, job.tenant_id, job.agent_name, _dumps(job.input), job.status.value,
            job.priority, job.retry_count, job.max_retries, job.current_step, job.progress,
            job.worker_id, _ts(job.lease_expire_at), job.cancel_requested, _ts(job.queued_at),
            _ts(job.started_at), _ts(job.finished_at),
            _dumps(job.result) if job.result is not None else None, job.error,
            _ts(job.updated_at),
        )
        return job

    async def transition(
        self, job_id: str, from_status: JobStatus, to_status: JobStatus, **fields: Any
    ) -> bool:
        sets = ["status=$2", "updated_at=$3"]
        params: list[Any] = [to_status.value, _ts(time.time())]
        named = {
            "current_step": None, "progress": None, "worker_id": None,
            "lease_expire_at": _ts, "error": None, "result": _dumps,
            "retry_count": None, "started_at": _ts, "finished_at": _ts,
            "cancel_requested": None,
        }
        for key, conv in named.items():
            if key in fields:
                value = fields[key]
                if conv is not None:
                    value = conv(value)
                sets.append(f"{key}=${len(params) + 1}")
                params.append(value)
        params.append(job_id)
        params.append(from_status.value)
        async with self.db._acq as conn:
            result = await conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id=${len(params)-1} AND status=${len(params)}",
                *params,
            )
        return result == "UPDATE 1"

    async def acquire_lease(self, job_id: str, worker_id: str, duration: float) -> bool:
        now = _ts(time.time())
        async with self.db._acq as conn:
            result = await conn.execute(
                """
                UPDATE jobs SET worker_id=$2, lease_expire_at=$3, updated_at=$3
                WHERE id=$1
                  AND (status IN ('queued','retrying')
                       OR lease_expire_at IS NULL
                       OR lease_expire_at < $3
                       OR worker_id = $2)
                """,
                job_id, worker_id, now,
            )
        return result == "UPDATE 1"

    async def renew_lease(self, job_id: str, worker_id: str, duration: float) -> bool:
        now = _ts(time.time())
        async with self.db._acq as conn:
            result = await conn.execute(
                "UPDATE jobs SET lease_expire_at=$3, updated_at=$3 WHERE id=$1 AND worker_id=$2",
                job_id, worker_id, now,
            )
        return result == "UPDATE 1"

    async def release_lease(self, job_id: str, worker_id: str) -> bool:
        async with self.db._acq as conn:
            result = await conn.execute(
                "UPDATE jobs SET worker_id=NULL, lease_expire_at=NULL, updated_at=$3 WHERE id=$1 AND worker_id=$2",
                job_id, worker_id, _ts(time.time()),
            )
        return result == "UPDATE 1"

    async def expire_lease(self, job_id: str, worker_id: str) -> bool:
        async with self.db._acq as conn:
            result = await conn.execute(
                "UPDATE jobs SET lease_expire_at=$3, updated_at=$3 WHERE id=$1 AND worker_id=$2",
                job_id, worker_id, _ts(time.time() - 1.0),
            )
        return result == "UPDATE 1"

    async def update_progress(
        self, job_id: str, worker_id: str, *, step: str | None, progress: int
    ) -> bool:
        async with self.db._acq as conn:
            if step is not None:
                result = await conn.execute(
                    "UPDATE jobs SET current_step=$3, progress=$4, updated_at=$5 WHERE id=$1 AND worker_id=$2",
                    job_id, worker_id, step, progress, _ts(time.time()),
                )
            else:
                result = await conn.execute(
                    "UPDATE jobs SET progress=$3, updated_at=$4 WHERE id=$1 AND worker_id=$2",
                    job_id, worker_id, progress, _ts(time.time()),
                )
        return result == "UPDATE 1"

    async def find_recoverable(self, now: float, grace: float) -> list[Job]:
        cutoff = _ts(now - grace)
        rows = await self._fetch(
            "SELECT * FROM jobs WHERE status IN ('running','retrying') AND lease_expire_at IS NOT NULL AND lease_expire_at < $1",
            cutoff,
        )
        return [self._row_to_job(r) for r in rows]

    async def set_cancel_requested(self, job_id: str, value: bool = True) -> bool:
        async with self.db._acq as conn:
            result = await conn.execute(
                "UPDATE jobs SET cancel_requested=$2, updated_at=$3 WHERE id=$1",
                job_id, value, _ts(time.time()),
            )
        return result == "UPDATE 1"

    async def is_cancel_requested(self, job_id: str) -> bool:
        row = await self._run("SELECT cancel_requested FROM jobs WHERE id=$1", job_id)
        return bool(row and row["cancel_requested"])

    async def list_dead(self) -> list[Job]:
        rows = await self._fetch("SELECT * FROM jobs WHERE status='dead' ORDER BY updated_at")
        return [self._row_to_job(r) for r in rows]

    async def count_by_status(self) -> dict[str, int]:
        rows = await self._fetch("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    async def close(self) -> None:
        return None

    @staticmethod
    def _row_to_job(row: Any) -> Job:
        return Job.from_dict(
            {
                "id": str(row["id"]),
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
                "lease_expire_at": _epoch(row["lease_expire_at"]),
                "cancel_requested": bool(row["cancel_requested"]),
                "queued_at": _epoch(row["queued_at"]),
                "started_at": _epoch(row["started_at"]),
                "finished_at": _epoch(row["finished_at"]),
                "result": _loads(row["result"]),
                "error": row["error"],
                "created_at": _epoch(row["created_at"]),
                "updated_at": _epoch(row["updated_at"]),
            }
        )


class PostgresEventStore(EventStore):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    async def append(self, job_id: str, event_type: str, payload: dict | None = None) -> JobEvent:
        async with self.db._acq as conn:
            row = await conn.fetchrow(
                "INSERT INTO job_events (job_id, event_type, payload, created_at) "
                "VALUES ($1,$2,$3,$4) RETURNING id",
                job_id, event_type, _dumps(payload) if payload is not None else None,
                _ts(time.time()),
            )
        ev = JobEvent(job_id=job_id, event_type=event_type, payload=payload)
        ev.seq = row["id"]
        return ev

    async def list(self, job_id: str) -> list[JobEvent]:
        rows = await self.db._fetch(
            "SELECT * FROM job_events WHERE job_id=$1 ORDER BY id", job_id
        )
        return [
            JobEvent(
                job_id=r["job_id"],
                event_type=r["event_type"],
                payload=_loads(r["payload"]),
                seq=r["id"],
                created_at=_epoch(r["created_at"]) or 0.0,
            )
            for r in rows
        ]

    async def close(self) -> None:
        return None
