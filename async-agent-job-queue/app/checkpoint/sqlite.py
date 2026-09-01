"""SQLite 版 CheckpointStore —— 复用共享 SqliteDatabase。"""
from __future__ import annotations

import json
import time
from typing import Any

from ..storage.sqlite import SqliteDatabase
from .base import CheckpointStore


class SqliteCheckpointStore(CheckpointStore):
    def __init__(self, db: SqliteDatabase) -> None:
        self.db = db

    async def save(self, job_id: str, checkpoint: dict) -> None:
        await self.db.execute(
            """
            INSERT INTO checkpoints (job_id, step, state, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                step=excluded.step, state=excluded.state, created_at=excluded.created_at
            """,
            (
                job_id,
                checkpoint.get("step"),
                json.dumps(checkpoint, ensure_ascii=False),
                time.time(),
            ),
        )
        await self.db.commit()

    async def load(self, job_id: str) -> dict | None:
        row = await self.db.fetchone(
            "SELECT state FROM checkpoints WHERE job_id = ?", (job_id,)
        )
        return json.loads(row["state"]) if row else None

    async def delete(self, job_id: str) -> None:
        await self.db.execute("DELETE FROM checkpoints WHERE job_id = ?", (job_id,))
        await self.db.commit()

    async def close(self) -> None:
        return None
