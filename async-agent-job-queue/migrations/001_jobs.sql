-- Job Store（设计说明书 §32）
-- 注意：Demo 默认用 SQLite（app/storage/sqlite.py 内联同构 DDL）。
-- 本文件为 PostgreSQL 方言，供 docker-compose 首次启动自动建表。

CREATE TABLE IF NOT EXISTS jobs (
    id               UUID PRIMARY KEY,
    tenant_id        VARCHAR(128) NOT NULL,
    agent_name       VARCHAR(128) NOT NULL,
    input            JSONB NOT NULL,
    status           VARCHAR(32) NOT NULL,
    priority         INT NOT NULL DEFAULT 0,
    retry_count      INT NOT NULL DEFAULT 0,
    max_retries      INT NOT NULL DEFAULT 3,
    current_step     VARCHAR(128),
    progress         INT NOT NULL DEFAULT 0,
    worker_id        VARCHAR(128),
    lease_expire_at  TIMESTAMP,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    queued_at        TIMESTAMP,
    started_at       TIMESTAMP,
    finished_at      TIMESTAMP,
    result           JSONB,
    error            TEXT,
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status       ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_lease        ON jobs (status, lease_expire_at);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_status ON jobs (tenant_id, status);
