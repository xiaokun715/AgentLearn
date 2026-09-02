"""Repository 接口 + SQLite 实现（设计说明书 §07, §09, §27）。

业务层只依赖 ``Repository`` 抽象；默认后端 SQLite（零外部依赖），
PostgreSQL 切换后实现同一接口即可（见 migrations/ 与 README）。
"""
from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import aiosqlite

from ..domain.delivery import (
    DELIVERING,
    Delivery,
    PENDING,
    RETRYING,
)
from ..domain.event import Event, utcnow
from ..domain.exceptions import ConflictError
from ..domain.outbox import OUTBOX_PENDING, OUTBOX_PROCESSING, OUTBOX_PUBLISHED, OutboxEntry
from ..domain.subscriber import Subscriber
from .models import SCHEMA, dumps, from_db, loads, to_db


class Repository(ABC):
    # ---- events -------------------------------------------------------------
    @abstractmethod
    async def create_event(self, event: Event) -> None: ...

    @abstractmethod
    async def get_event(self, event_id: str) -> Event | None: ...

    @abstractmethod
    async def list_events(self, *, limit: int = 100) -> list[Event]: ...

    # ---- subscribers --------------------------------------------------------
    @abstractmethod
    async def create_subscriber(self, subscriber: Subscriber) -> None: ...

    @abstractmethod
    async def get_subscriber(self, subscriber_id: str) -> Subscriber | None: ...

    @abstractmethod
    async def list_subscribers(self, *, tenant_id: str | None = None) -> list[Subscriber]: ...

    @abstractmethod
    async def set_subscriber_status(self, subscriber_id: str, status: str) -> None: ...

    @abstractmethod
    async def replace_subscriber_events(self, subscriber_id: str, events: list[str]) -> None: ...

    @abstractmethod
    async def match_subscribers(self, event_type: str, *, tenant_id: str | None = None) -> list[Subscriber]: ...

    # ---- deliveries ---------------------------------------------------------
    @abstractmethod
    async def create_delivery(self, delivery: Delivery) -> None: ...

    @abstractmethod
    async def get_delivery(self, delivery_id: str) -> Delivery | None: ...

    @abstractmethod
    async def find_delivery(self, event_id: str, subscriber_id: str) -> Delivery | None: ...

    @abstractmethod
    async def list_deliveries(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[Delivery]: ...

    @abstractmethod
    async def claim_delivery(self, delivery_id: str, now: datetime) -> Delivery | None:
        """原子领取单个 Delivery（PENDING/RETRYING -> DELIVERING，attempt+1）。

        已被领取（状态已变）返回 None —— 这是实验 5「重复消费」的兜底。
        """

    @abstractmethod
    async def claim_due_deliveries(self, batch_size: int, now: datetime) -> list[Delivery]:
        """原子领取一批到期（PENDING / RETRYING 且 next_retry_at <= now）的 Delivery。"""

    @abstractmethod
    async def update_delivery(self, delivery: Delivery) -> None: ...

    # ---- outbox -------------------------------------------------------------
    @abstractmethod
    async def create_outbox(self, entry: OutboxEntry) -> None: ...

    @abstractmethod
    async def claim_outbox_entries(self, batch_size: int) -> list[OutboxEntry]: ...

    @abstractmethod
    async def mark_outbox(self, entry_id: str, status: str) -> None: ...

    @abstractmethod
    async def count_outbox_pending(self) -> int: ...

    # ---- 原子操作（§26） ------------------------------------------------------
    @abstractmethod
    async def create_event_with_outbox(self, event: Event, entry: OutboxEntry) -> None:
        """事件 + Outbox 同一事务提交 —— Outbox Pattern 的核心。"""

    async def close(self) -> None:  # noqa: B027
        return None


class _AsyncRLock:
    """可重入异步锁：同一任务可多次 acquire，不同任务互斥。

    共享单连接必须串行化 —— 否则 worker 协程与请求协程会在
    「UPDATE..RETURNING 后、显式 COMMIT 前」交错，导致
    ``cannot commit - no transaction is active``（多协程竞争一个连接）。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._count = 0

    async def acquire(self) -> None:
        me = asyncio.current_task()
        if me is self._owner:
            self._count += 1
            return
        await self._lock.acquire()
        self._owner = me
        self._count = 1

    def release(self) -> None:
        assert self._owner is asyncio.current_task()
        self._count -= 1
        if self._count == 0:
            self._owner = None
            self._lock.release()


class SqliteDatabase:
    """共享单连接 + WAL，封装常用异步查询与事务。

    所有读写经 ``_guard``（可重入锁）串行化，保证单连接上的事务原子性
    （多 Worker / 多请求协程共享同一连接时依然正确）。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._guard = _AsyncRLock()
        self._tx_depth = 0  # 处于 transaction() 内的层数

    async def connect(self) -> None:
        if self.path != ":memory:":
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA)
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

    async def execute(self, sql: str, *params: Any) -> int:
        await self._guard.acquire()
        try:
            try:
                cur = await self._require().execute(sql, params)
                # 不在事务上下文内才自动提交（否则会把 transaction() 的事务提前提交）
                if self._tx_depth == 0:
                    await self._require().commit()
                return cur.rowcount
            except BaseException:
                # 约束违反等错误会让 sqlite3 留下未提交的隐式事务（in_transaction=True）。
                # 不回滚的话，下一次 BEGIN 会抛 "cannot start a transaction within a transaction"。
                if self._tx_depth == 0:
                    await self._require().rollback()
                raise
        finally:
            self._guard.release()

    async def fetchone(self, sql: str, *params: Any) -> aiosqlite.Row | None:
        await self._guard.acquire()
        try:
            async with self._require().execute(sql, params) as cur:
                return await cur.fetchone()
        finally:
            self._guard.release()

    async def fetchall(self, sql: str, *params: Any) -> list[aiosqlite.Row]:
        await self._guard.acquire()
        try:
            async with self._require().execute(sql, params) as cur:
                return await cur.fetchall()
        finally:
            self._guard.release()

    async def fetchall_in_tx(self, sql: str, *params: Any) -> list[aiosqlite.Row]:
        """事务内执行（不自动 commit，供 UPDATE...RETURNING 后显式 COMMIT）。"""
        await self._guard.acquire()
        try:
            async with self._require().execute(sql, params) as cur:
                return await cur.fetchall()
        finally:
            self._guard.release()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["SqliteDatabase"]:
        """显式事务：begin ... commit / rollback。

        整个事务持有可重入锁，其他协程的读写被挡在外面，
        保证多语句（如 Event + Outbox）真正原子提交（§26）。

        锁的 acquire/release 包围整个事务（含 BEGIN）——
        BEGIN 失败也必须释放锁，否则系统永久死锁。
        """
        await self._guard.acquire()
        try:
            conn = self._require()
            await conn.execute("BEGIN")
            self._tx_depth += 1
            try:
                yield self
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            finally:
                self._tx_depth -= 1
        finally:
            self._guard.release()


# ---------------------------------------------------------------------------
# SQLite Repository
# ---------------------------------------------------------------------------
class SqliteRepository(Repository):
    def __init__(self, db: SqliteDatabase) -> None:
        self.db = db

    # ---- events ------------------------------------------------------------
    async def create_event(self, event: Event) -> None:
        await self.db.execute(
            "INSERT INTO events (id, type, tenant_id, data, metadata, created_at)"
            " VALUES (?,?,?,?,?,?)",
            event.id, event.type, event.tenant_id,
            dumps(event.data), dumps(event.metadata), to_db(event.created_at),
        )

    async def get_event(self, event_id: str) -> Event | None:
        row = await self.db.fetchone("SELECT * FROM events WHERE id=?", event_id)
        return _row_to_event(row) if row else None

    async def list_events(self, *, limit: int = 100) -> list[Event]:
        rows = await self.db.fetchall(
            "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", limit
        )
        return [_row_to_event(r) for r in rows]

    # ---- subscribers -------------------------------------------------------
    async def create_subscriber(self, subscriber: Subscriber) -> None:
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO subscribers (id, tenant_id, url, secret, status, created_at)"
                " VALUES (?,?,?,?,?,?)",
                subscriber.id, subscriber.tenant_id, subscriber.url,
                subscriber.secret, subscriber.status, to_db(subscriber.created_at),
            )
            for ev in subscriber.events:
                await self.db.execute(
                    "INSERT OR IGNORE INTO subscriber_events (subscriber_id, event_type)"
                    " VALUES (?,?)",
                    subscriber.id, ev,
                )

    async def get_subscriber(self, subscriber_id: str) -> Subscriber | None:
        row = await self.db.fetchone(
            "SELECT * FROM subscribers WHERE id=?", subscriber_id
        )
        if row is None:
            return None
        sub = _row_to_subscriber(row)
        sub.events = await self._subscriber_events(subscriber_id)
        return sub

    async def list_subscribers(self, *, tenant_id: str | None = None) -> list[Subscriber]:
        if tenant_id:
            rows = await self.db.fetchall(
                "SELECT * FROM subscribers WHERE tenant_id=? ORDER BY created_at", tenant_id
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM subscribers ORDER BY created_at"
            )
        result = []
        for r in rows:
            sub = _row_to_subscriber(r)
            sub.events = await self._subscriber_events(sub.id)
            result.append(sub)
        return result

    async def set_subscriber_status(self, subscriber_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE subscribers SET status=? WHERE id=?", status, subscriber_id
        )

    async def replace_subscriber_events(self, subscriber_id: str, events: list[str]) -> None:
        async with self.db.transaction():
            await self.db.execute(
                "DELETE FROM subscriber_events WHERE subscriber_id=?", subscriber_id
            )
            for ev in events:
                await self.db.execute(
                    "INSERT OR IGNORE INTO subscriber_events (subscriber_id, event_type)"
                    " VALUES (?,?)",
                    subscriber_id, ev,
                )

    async def match_subscribers(
        self, event_type: str, *, tenant_id: str | None = None
    ) -> list[Subscriber]:
        """Fan-out 匹配（§08, §33）：订阅了该事件类型且状态 active 的 Subscriber。"""
        sql = (
            "SELECT s.* FROM subscribers s"
            " JOIN subscriber_events se ON se.subscriber_id = s.id"
            " WHERE se.event_type=? AND s.status='active'"
        )
        params: list[Any] = [event_type]
        if tenant_id:
            sql += " AND s.tenant_id=?"
            params.append(tenant_id)
        rows = await self.db.fetchall(sql, *params)
        result = []
        for r in rows:
            sub = _row_to_subscriber(r)
            sub.events = await self._subscriber_events(sub.id)
            result.append(sub)
        return result

    async def _subscriber_events(self, subscriber_id: str) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT event_type FROM subscriber_events WHERE subscriber_id=?",
            subscriber_id,
        )
        return [r["event_type"] for r in rows]

    # ---- deliveries --------------------------------------------------------
    async def create_delivery(self, delivery: Delivery) -> None:
        try:
            await self.db.execute(
                "INSERT INTO webhook_deliveries"
                " (id, event_id, subscriber_id, status, attempt_count, next_retry_at,"
                "  last_error, response_status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                delivery.id, delivery.event_id, delivery.subscriber_id, delivery.status,
                delivery.attempt_count,
                to_db(delivery.next_retry_at) if delivery.next_retry_at else None,
                delivery.last_error, delivery.response_status,
                to_db(delivery.created_at), to_db(delivery.updated_at),
            )
        except aiosqlite.IntegrityError as e:
            raise ConflictError(
                f"Delivery(event={delivery.event_id}, subscriber={delivery.subscriber_id}) 已存在，"
                "UNIQUE(event_id, subscriber_id) 保证同一事件不会重复投递（§09）"
            ) from e

    async def get_delivery(self, delivery_id: str) -> Delivery | None:
        row = await self.db.fetchone(
            "SELECT * FROM webhook_deliveries WHERE id=?", delivery_id
        )
        return _row_to_delivery(row) if row else None

    async def find_delivery(self, event_id: str, subscriber_id: str) -> Delivery | None:
        row = await self.db.fetchone(
            "SELECT * FROM webhook_deliveries WHERE event_id=? AND subscriber_id=?",
            event_id, subscriber_id,
        )
        return _row_to_delivery(row) if row else None

    async def list_deliveries(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[Delivery]:
        if status:
            rows = await self.db.fetchall(
                "SELECT * FROM webhook_deliveries WHERE status=? ORDER BY created_at DESC LIMIT ?",
                status, limit,
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM webhook_deliveries ORDER BY created_at DESC LIMIT ?", limit
            )
        return [_row_to_delivery(r) for r in rows]

    async def claim_delivery(self, delivery_id: str, now: datetime) -> Delivery | None:
        """条件领取：仅当仍为 PENDING/RETRYING 才置为 DELIVERING 并 attempt+1。

        用显式事务包住 UPDATE..RETURNING，保证「领取 + 提交」原子，
        多个 worker 并发消费同一 Delivery 时只有一个成功（§09 幂等）。
        """
        async with self.db.transaction():
            rows = await self.db.fetchall_in_tx(
                "UPDATE webhook_deliveries"
                " SET status=?, attempt_count=attempt_count+1, updated_at=?"
                " WHERE id=? AND status IN (?,?)"
                " RETURNING *",
                DELIVERING, to_db(now), delivery_id, PENDING, RETRYING,
            )
        if not rows:
            return None
        return _row_to_delivery(rows[0])

    async def claim_due_deliveries(
        self, batch_size: int, now: datetime, *, claim_timeout: float = 120.0
    ) -> list[Delivery]:
        """领取到期 Delivery（PENDING / RETRYING 且到期）。

        同时回收卡在 DELIVERING 超过租约的 Delivery（§34 at-least-once 的兜底）：
        Worker 崩溃/异常导致投递中途丢失时，超过 ``claim_timeout`` 会被重新领取重投。
        """
        async with self.db.transaction():
            rows = await self.db.fetchall_in_tx(
                "UPDATE webhook_deliveries"
                " SET status=?, attempt_count=attempt_count+1, updated_at=?"
                " WHERE id IN ("
                "   SELECT id FROM webhook_deliveries"
                "   WHERE (status IN (?,?) AND (next_retry_at IS NULL OR next_retry_at <= ?))"
                "      OR (status=? AND updated_at <= ?)"
                "   ORDER BY created_at LIMIT ?"
                " ) RETURNING *",
                DELIVERING, to_db(now),
                PENDING, RETRYING, to_db(now),
                DELIVERING, to_db(now - timedelta(seconds=claim_timeout)),
                batch_size,
            )
        return [_row_to_delivery(r) for r in rows]

    async def update_delivery(self, delivery: Delivery) -> None:
        await self.db.execute(
            "UPDATE webhook_deliveries"
            " SET status=?, attempt_count=?, next_retry_at=?, last_error=?,"
            "     response_status=?, updated_at=?"
            " WHERE id=?",
            delivery.status, delivery.attempt_count,
            to_db(delivery.next_retry_at) if delivery.next_retry_at else None,
            delivery.last_error, delivery.response_status,
            to_db(delivery.updated_at), delivery.id,
        )

    # ---- outbox ------------------------------------------------------------
    async def create_outbox(self, entry: OutboxEntry) -> None:
        await self.db.execute(
            "INSERT INTO outbox_events"
            " (id, event_id, status, created_at, updated_at, published_at)"
            " VALUES (?,?,?,?,?,?)",
            entry.id, entry.event_id, entry.status,
            to_db(entry.created_at), to_db(entry.updated_at),
            to_db(entry.published_at) if entry.published_at else None,
        )

    async def claim_outbox_entries(
        self, batch_size: int, *, claim_timeout: float = 120.0
    ) -> list[OutboxEntry]:
        """原子领取待发布 Outbox；同时回收卡在 PROCESSING 超过租约的条目。"""
        now = utcnow()
        async with self.db.transaction():
            rows = await self.db.fetchall_in_tx(
                "UPDATE outbox_events SET status=?, updated_at=?"
                " WHERE id IN ("
                "   SELECT id FROM outbox_events"
                "   WHERE status=? OR (status=? AND updated_at <= ?)"
                "   ORDER BY created_at LIMIT ?"
                " ) RETURNING *",
                OUTBOX_PROCESSING, to_db(now),
                OUTBOX_PENDING, OUTBOX_PROCESSING, to_db(now - timedelta(seconds=claim_timeout)),
                batch_size,
            )
        return [_row_to_outbox(r) for r in rows]

    async def mark_outbox(self, entry_id: str, status: str) -> None:
        now = utcnow()
        if status == OUTBOX_PUBLISHED:
            await self.db.execute(
                "UPDATE outbox_events SET status=?, updated_at=?, published_at=?"
                " WHERE id=?",
                status, to_db(now), to_db(now), entry_id,
            )
        else:
            await self.db.execute(
                "UPDATE outbox_events SET status=?, updated_at=?, published_at=NULL"
                " WHERE id=?",
                status, to_db(now), entry_id,
            )

    async def count_outbox_pending(self) -> int:
        # 包含 PROCESSING：卡住的条目也不该在健康检查中「隐身」
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM outbox_events WHERE status IN (?,?)",
            OUTBOX_PENDING, OUTBOX_PROCESSING,
        )
        return int(row["n"])

    # ---- 原子操作（§26） ------------------------------------------------------
    async def create_event_with_outbox(self, event: Event, entry: OutboxEntry) -> None:
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO events (id, type, tenant_id, data, metadata, created_at)"
                " VALUES (?,?,?,?,?,?)",
                event.id, event.type, event.tenant_id,
                dumps(event.data), dumps(event.metadata), to_db(event.created_at),
            )
            await self.db.execute(
                "INSERT INTO outbox_events"
                " (id, event_id, status, created_at, updated_at, published_at)"
                " VALUES (?,?,?,?,?,?)",
                entry.id, entry.event_id, entry.status,
                to_db(entry.created_at), to_db(entry.updated_at), None,
            )


# ---------------------------------------------------------------------------
# row -> domain
# ---------------------------------------------------------------------------
def _row_to_event(row: aiosqlite.Row) -> Event:
    return Event(
        id=row["id"],
        type=row["type"],
        tenant_id=row["tenant_id"],
        created_at=from_db(row["created_at"]),
        data=loads(row["data"]) or {},
        metadata=loads(row["metadata"]) or {},
    )


def _row_to_subscriber(row: aiosqlite.Row) -> Subscriber:
    return Subscriber(
        id=row["id"],
        tenant_id=row["tenant_id"],
        url=row["url"],
        secret=row["secret"],
        events=[],
        status=row["status"],
        created_at=from_db(row["created_at"]),
    )


def _row_to_delivery(row: aiosqlite.Row) -> Delivery:
    return Delivery(
        id=row["id"],
        event_id=row["event_id"],
        subscriber_id=row["subscriber_id"],
        status=row["status"],
        attempt_count=row["attempt_count"],
        next_retry_at=from_db(row["next_retry_at"]),
        last_error=row["last_error"],
        response_status=row["response_status"],
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
    )


def _row_to_outbox(row: aiosqlite.Row) -> OutboxEntry:
    return OutboxEntry(
        id=row["id"],
        event_id=row["event_id"],
        status=row["status"],
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]) or from_db(row["created_at"]),
        published_at=from_db(row["published_at"]),
    )
