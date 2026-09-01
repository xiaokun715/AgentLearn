"""存储模型 —— 表结构 DDL 与序列化辅助（设计说明书 §07, §09, §27）。

SQLite 是默认后端（零外部依赖），DDL 与 migrations/001_init_postgres.sql
（PostgreSQL 方言）同构。PostgreSQL 可直接通过 ``DATABASE_URL`` 切换。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# ---- 建表 DDL（SQLite 方言） -------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    data       TEXT NOT NULL,
    metadata   TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscribers (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    url        TEXT NOT NULL,
    secret     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

-- 设计 §07：Subscriber 与事件类型的多对多关系
CREATE TABLE IF NOT EXISTS subscriber_events (
    subscriber_id TEXT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    event_type    TEXT NOT NULL,
    PRIMARY KEY (subscriber_id, event_type)
);

-- 设计 §09：系统核心表。UNIQUE(event_id, subscriber_id) 保证幂等
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    subscriber_id   TEXT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    status          TEXT NOT NULL,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    next_retry_at   TEXT,
    last_error      TEXT,
    response_status INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (event_id, subscriber_id)
);

-- 设计 §26~§27：Outbox Pattern
CREATE TABLE IF NOT EXISTS outbox_events (
    id           TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    published_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_deliveries_status   ON webhook_deliveries (status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_outbox_status       ON outbox_events (status);
CREATE INDEX IF NOT EXISTS idx_events_created_at   ON events (created_at);
"""


def dumps(value: Any) -> str:
    """dict/list -> JSON text（紧凑、可排序）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(raw: str | None) -> Any:
    return json.loads(raw) if raw else None


def to_db(dt: datetime) -> str:
    """datetime -> ISO 8601 字符串（带时区）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def from_db(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None
