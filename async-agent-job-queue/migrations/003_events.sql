-- Event Store（设计说明书 §34-36）
-- 只追加（append-only）的事件日志：State = 快照，Event = 历史。

CREATE TABLE IF NOT EXISTS job_events (
    id         BIGSERIAL PRIMARY KEY,
    job_id     UUID NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    payload    JSONB,
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events (job_id, id);
