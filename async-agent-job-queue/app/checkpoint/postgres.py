"""PostgreSQL 版 CheckpointStore（可选后端）。"""
from __future__ import annotations

import json
import time
from typing import Any

from ..storage.postgres import PostgresDatabase
from .base import CheckpointStore


class PostgresCheckpointStore(CheckpointStore):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    async def save(self, job_id: str, checkpoint: dict) -> None:
        async with self.db._acq as conn:
            await conn.execute(
                """
                INSERT INTO checkpoints (job_id, step, state, created_at)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT(job_id) DO UPDATE SET
                    step=excluded.step, state=excluded.state, created_at=excluded.created_at
                """,
                job_id,
                checkpoint.get("step"),
                json.dumps(checkpoint, ensure_ascii=False),
                time.time(),
            )

    async def load(self, job_id: str) -> dict | None:
        async with self.db._acq as conn:
            row = await conn.fetchrow(
                "SELECT state FROM checkpoints WHERE job_id=$1", job_id
            )
        return json.loads(row["state"]) if row else None

    async def delete(self, job_id: str) -> None:
        async with self.db._acq as conn:
            await conn.execute("DELETE FROM checkpoints WHERE job_id=$1", job_id)

    async def close(self) -> None:
        return None
