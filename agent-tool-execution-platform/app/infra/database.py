"""持久层 —— PostgreSQL 的替身（说明书 §42 / §44 / §45 / §46）。

角色定位（§44）：**PostgreSQL 是 Durable Source of Truth，Redis 只是 Cache / Coordination。**
所有「执行发生过」「结果是什么」的最终事实都落在这里。

为什么 Demo 用 SQLite 而不是直接连 PG
-------------------------------------
本模块刻意只用 ``sqlite3``（stdlib），换取「克隆下来就能跑」。但把 PG 真正在用到的
三件事都保留了，因为它们**不是可选优化，而是正确性的一部分**：

1. **真事务**（§45）。结果与状态必须一起提交：不能出现 Redis 说 SUCCESS、
   DB 还说 PROCESSING 的撕裂 —— 否则 Agent 拿着 SUCCESS 去查结果却查不到。
   :meth:`Database.finalize_success` / :meth:`Database.finalize_failure`
   把「更新 execution + 写 result + 追加 outbox 事件」放进同一个事务。

2. **Outbox**（§46）。DB 提交成功之后才由投递器驱动 Redis 更新，做到最终一致。
   这样即使进程在「DB 已提交、Redis 还没更新」的瞬间挂掉，重启后
   :meth:`Database.fetch_unpublished_outbox` 仍能把消息补发出去。

3. **审计事件流**（§42 ``execution_event``）。用于 audit / debug / recovery / observability。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional

from ..domain.enums import ExecutionStatus
from ..domain.models import (
    ExecutionEvent,
    ExecutionRecord,
    OutboxRecord,
    ToolResultRecord,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_execution (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id           TEXT    NOT NULL UNIQUE,
    run_id            TEXT    NOT NULL DEFAULT '',
    agent_id          TEXT    NOT NULL DEFAULT '',
    tenant_id         TEXT    NOT NULL DEFAULT 'tenantA',
    tool_name         TEXT    NOT NULL,
    tool_version      TEXT    NOT NULL DEFAULT '1.0',
    idempotency_key   TEXT    NOT NULL,
    arguments         TEXT    NOT NULL DEFAULT '{}',
    arguments_hash    TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT 'CREATED',
    attempt           INTEGER NOT NULL DEFAULT 0,
    worker_id         TEXT,
    lease_id          TEXT,
    started_at        TEXT,
    finished_at       TEXT,
    error_type        TEXT,
    error_message     TEXT,
    result_id         TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exec_call        ON tool_execution(call_id);
CREATE INDEX IF NOT EXISTS idx_exec_idem        ON tool_execution(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_exec_run         ON tool_execution(run_id);
CREATE INDEX IF NOT EXISTS idx_exec_status      ON tool_execution(status);

CREATE TABLE IF NOT EXISTS tool_result (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id        TEXT    NOT NULL,
    result_type    TEXT    NOT NULL DEFAULT 'inline',
    inline_result  TEXT,
    artifact_id    TEXT,
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    content_hash   TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_result_call ON tool_result(call_id);

CREATE TABLE IF NOT EXISTS execution_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id     TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    payload     TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_call ON execution_event(call_id, id);

CREATE TABLE IF NOT EXISTS outbox_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregate_id  TEXT    NOT NULL,
    event_type    TEXT    NOT NULL,
    payload       TEXT    NOT NULL DEFAULT '{}',
    published     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    published_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON outbox_event(published, id);
"""


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


class Database:
    """平台唯一的事实来源。

    线程安全策略：``sqlite3`` 连接加锁 —— 平台的核心路径是单线程 asyncio，
    但 Worker/Reaper 可能跑在线程里，这把锁保证不会串行写坏。
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL 让读写不互斥，更接近 PG 的并发观感
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ==================================================================
    # 事务原语
    # ==================================================================
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务。

        平台里所有「一次提交多个事实」的写操作都必须走这里 —— 说明书 §45 的
        「结果持久化必须 Result + Execution Status 一起提交」不是接口偏好，是硬约束。
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ==================================================================
    # tool_execution
    # ==================================================================
    def create_execution(self, record: ExecutionRecord) -> ExecutionRecord:
        """落一条 ``CREATED`` 记录（§41 状态机起点）。"""
        with self.transaction() as conn:
            self._insert_execution(conn, record)
        return record

    @staticmethod
    def _insert_execution(conn: sqlite3.Connection, record: ExecutionRecord) -> None:
        conn.execute(
            """
            INSERT INTO tool_execution (
                call_id, run_id, agent_id, tenant_id, tool_name, tool_version,
                idempotency_key, arguments, arguments_hash, status, attempt,
                worker_id, lease_id, started_at, finished_at,
                error_type, error_message, result_id, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.call_id,
                record.run_id,
                record.agent_id,
                record.tenant_id,
                record.tool_name,
                record.tool_version,
                record.idempotency_key,
                json.dumps(record.arguments, ensure_ascii=False, default=str),
                record.arguments_hash,
                record.status.value,
                record.attempt,
                record.worker_id,
                record.lease_id,
                _iso(record.started_at),
                _iso(record.finished_at),
                record.error_type,
                record.error_message,
                record.result_id,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )

    def upsert_execution(self, record: ExecutionRecord) -> None:
        """存在则更新、不存在则插入 —— 幂等写，便于恢复路径反复调用。"""
        existing = self.get_execution(record.call_id)
        if existing is None:
            self.create_execution(record)
        else:
            self.update_execution(
                record.call_id,
                status=record.status,
                attempt=record.attempt,
                worker_id=record.worker_id,
                lease_id=record.lease_id,
                started_at=record.started_at,
                finished_at=record.finished_at,
                result_id=record.result_id,
                error_type=record.error_type,
                error_message=record.error_message,
            )

    _UPDATABLE = {
        "status",
        "attempt",
        "worker_id",
        "lease_id",
        "started_at",
        "finished_at",
        "error_type",
        "error_message",
        "result_id",
    }

    def update_execution(self, call_id: str, **fields: Any) -> None:
        """按列更新执行记录；``status`` 可传枚举或字符串。"""
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            raise ValueError(f"不可更新的列: {sorted(unknown)}")

        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if isinstance(value, ExecutionStatus):
                value = value.value
            elif isinstance(value, datetime):
                value = value.isoformat()
            sets.append(f"{key} = ?")
            params.append(value)
        sets.append("updated_at = ?")
        params.append(utcnow().isoformat())
        params.append(call_id)

        with self.transaction() as conn:
            conn.execute(
                f"UPDATE tool_execution SET {', '.join(sets)} WHERE call_id = ?", params
            )

    def get_execution(self, call_id: str) -> Optional[ExecutionRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tool_execution WHERE call_id = ?", (call_id,)
            ).fetchone()
        return self._row_to_execution(row) if row else None

    def get_execution_by_idempotency_key(self, key: str) -> Optional[ExecutionRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tool_execution WHERE idempotency_key = ? "
                "ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
        return self._row_to_execution(row) if row else None

    def list_executions(self, run_id: Optional[str] = None) -> list[ExecutionRecord]:
        sql = "SELECT * FROM tool_execution"
        params: tuple = ()
        if run_id is not None:
            sql += " WHERE run_id = ?"
            params = (run_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_execution(r) for r in rows]

    @staticmethod
    def _row_to_execution(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            id=row["id"],
            call_id=row["call_id"],
            run_id=row["run_id"],
            agent_id=row["agent_id"],
            tenant_id=row["tenant_id"],
            tool_name=row["tool_name"],
            tool_version=row["tool_version"],
            idempotency_key=row["idempotency_key"],
            arguments=json.loads(row["arguments"] or "{}"),
            arguments_hash=row["arguments_hash"],
            status=ExecutionStatus(row["status"]),
            attempt=row["attempt"],
            worker_id=row["worker_id"],
            lease_id=row["lease_id"],
            started_at=_parse(row["started_at"]),
            finished_at=_parse(row["finished_at"]),
            error_type=row["error_type"],
            error_message=row["error_message"],
            result_id=row["result_id"],
            created_at=_parse(row["created_at"]) or utcnow(),
            updated_at=_parse(row["updated_at"]) or utcnow(),
        )

    # ==================================================================
    # tool_result
    # ==================================================================
    def save_result(self, record: ToolResultRecord) -> ToolResultRecord:
        """单独写结果（非终态路径用；终态请用 :meth:`finalize_success`）。"""
        with self.transaction() as conn:
            self._insert_result(conn, record)
        return record

    @staticmethod
    def _insert_result(conn: sqlite3.Connection, record: ToolResultRecord) -> int:
        cursor = conn.execute(
            """
            INSERT INTO tool_result (
                call_id, result_type, inline_result, artifact_id,
                size_bytes, content_hash, created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                record.call_id,
                record.result_type,
                record.inline_result,
                record.artifact_id,
                record.size_bytes,
                record.content_hash,
                record.created_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def get_result(self, call_id: str) -> Optional[ToolResultRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tool_result WHERE call_id = ? ORDER BY id DESC LIMIT 1",
                (call_id,),
            ).fetchone()
        if row is None:
            return None
        return ToolResultRecord(
            id=row["id"],
            call_id=row["call_id"],
            result_type=row["result_type"],
            inline_result=row["inline_result"],
            artifact_id=row["artifact_id"],
            size_bytes=row["size_bytes"],
            content_hash=row["content_hash"],
            created_at=_parse(row["created_at"]) or utcnow(),
        )

    # ==================================================================
    # 事务化终态提交（§44 / §45 / §46）
    # ==================================================================
    def finalize_success(
        self,
        *,
        call_id: str,
        result: Optional[ToolResultRecord],
        events: Optional[list[ExecutionEvent]] = None,
    ) -> Optional[ToolResultRecord]:
        """把「执行成功」作为一个**原子事实**写下去。

        一个事务里同时完成（§44/§45/§46）::

            tool_execution   -> SUCCESS + result_id
            tool_result      -> 结果本体
            execution_event  -> 审计
            outbox_event     -> 待投递给 Redis 的「已经 SUCCESS」通知

        **顺序是关键**：先 DB 提交，再由 Outbox 投递器更新 Redis。
        反过来做就会出现「Redis 说成功、DB 查不到」的撕裂窗口。
        """
        events = events or []
        with self.transaction() as conn:
            result_id: Optional[str] = None
            if result is not None:
                new_id = self._insert_result(conn, result)
                result_id = f"result_{new_id}"
                result.id = new_id
            conn.execute(
                """
                UPDATE tool_execution
                   SET status = ?, result_id = ?, finished_at = ?, updated_at = ?
                 WHERE call_id = ?
                """,
                (
                    ExecutionStatus.SUCCESS.value,
                    result_id,
                    utcnow().isoformat(),
                    utcnow().isoformat(),
                    call_id,
                ),
            )
            for event in events:
                self._insert_event(conn, event)
            self._insert_outbox(
                conn,
                OutboxRecord(
                    aggregate_id=call_id,
                    event_type="tool.completed",
                    payload={"call_id": call_id, "status": "success", "result_id": result_id},
                ),
            )
        return result

    def finalize_failure(
        self,
        *,
        call_id: str,
        error_type: str,
        error_message: str,
        status: ExecutionStatus = ExecutionStatus.FAILED,
        events: Optional[list[ExecutionEvent]] = None,
    ) -> None:
        """失败终态提交 —— 与成功一样，状态 + 审计 + Outbox 同事务。"""
        events = events or []
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE tool_execution
                   SET status = ?, error_type = ?, error_message = ?,
                       finished_at = ?, updated_at = ?
                 WHERE call_id = ?
                """,
                (
                    status.value,
                    error_type,
                    error_message,
                    utcnow().isoformat(),
                    utcnow().isoformat(),
                    call_id,
                ),
            )
            for event in events:
                self._insert_event(conn, event)
            self._insert_outbox(
                conn,
                OutboxRecord(
                    aggregate_id=call_id,
                    event_type="tool.failed",
                    payload={
                        "call_id": call_id,
                        "status": status.value,
                        "error_type": error_type,
                        "error_message": error_message,
                    },
                ),
            )

    # ==================================================================
    # execution_event（审计 / 排障 / 恢复 / 可观测性）
    # ==================================================================
    def append_event(self, call_id: str, event_type: str, payload: dict | None = None) -> None:
        with self.transaction() as conn:
            self._insert_event(
                conn,
                ExecutionEvent(call_id=call_id, event_type=event_type, payload=payload or {}),
            )

    @staticmethod
    def _insert_event(conn: sqlite3.Connection, event: ExecutionEvent) -> None:
        conn.execute(
            "INSERT INTO execution_event (call_id, event_type, payload, created_at) VALUES (?,?,?,?)",
            (
                event.call_id,
                event.event_type,
                json.dumps(event.payload, ensure_ascii=False, default=str),
                event.created_at.isoformat(),
            ),
        )

    def list_events(self, call_id: str) -> list[ExecutionEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM execution_event WHERE call_id = ? ORDER BY id", (call_id,)
            ).fetchall()
        return [
            ExecutionEvent(
                id=r["id"],
                call_id=r["call_id"],
                event_type=r["event_type"],
                payload=json.loads(r["payload"] or "{}"),
                created_at=_parse(r["created_at"]) or utcnow(),
            )
            for r in rows
        ]

    # ==================================================================
    # Outbox（§46）
    # ==================================================================
    @staticmethod
    def _insert_outbox(conn: sqlite3.Connection, record: OutboxRecord) -> None:
        conn.execute(
            """
            INSERT INTO outbox_event (aggregate_id, event_type, payload, published, created_at)
            VALUES (?,?,?,0,?)
            """,
            (
                record.aggregate_id,
                record.event_type,
                json.dumps(record.payload, ensure_ascii=False, default=str),
                record.created_at.isoformat(),
            ),
        )

    def fetch_unpublished_outbox(self, limit: int = 100) -> list[OutboxRecord]:
        """取未投递的 Outbox 事件 —— 投递器（或崩溃重启后的补偿任务）调用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outbox_event WHERE published = 0 ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            OutboxRecord(
                id=r["id"],
                aggregate_id=r["aggregate_id"],
                event_type=r["event_type"],
                payload=json.loads(r["payload"] or "{}"),
                published=bool(r["published"]),
                created_at=_parse(r["created_at"]) or utcnow(),
                published_at=_parse(r["published_at"]),
            )
            for r in rows
        ]

    def mark_outbox_published(self, outbox_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE outbox_event SET published = 1, published_at = ? WHERE id = ?",
                (utcnow().isoformat(), outbox_id),
            )

    # ==================================================================
    # 统计 / 运维
    # ==================================================================
    def count_by_status(self) -> dict[str, int]:
        """各状态计数 —— 观测 API / 演示收尾都用它。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tool_execution GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
