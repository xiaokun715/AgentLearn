-- 设计说明书 §07, §09, §27 —— PostgreSQL 方言初始迁移
-- 与 app/storage/models.py 的 SQLite DDL 同构。
-- 使用方式（可选后端）：
--   psql "$DATABASE_URL" -f migrations/001_init_postgres.sql

CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    data       JSONB NOT NULL DEFAULT '{}',
    metadata   JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS subscribers (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    url        TEXT NOT NULL,
    secret     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL
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
    status          VARCHAR(32) NOT NULL,
    attempt_count   INT NOT NULL DEFAULT 0,
    next_retry_at   TIMESTAMPTZ,
    last_error      TEXT,
    response_status INT,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (event_id, subscriber_id)
);

-- 设计 §26~§27：Outbox Pattern
CREATE TABLE IF NOT EXISTS outbox_events (
    id           TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    status       VARCHAR(32) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_deliveries_status ON webhook_deliveries (status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_outbox_status     ON outbox_events (status);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at);
