-- Checkpoint Store（设计说明书 §33）
-- 每个 Job 只保留「最新」一个安全恢复点（job_id 为主键）。
-- state 中携带 completed_steps / tool_records，用于断点续跑与 Tool 幂等。

CREATE TABLE IF NOT EXISTS checkpoints (
    job_id     UUID PRIMARY KEY,
    step       VARCHAR(128) NOT NULL,
    state      JSONB NOT NULL,
    created_at TIMESTAMP NOT NULL
);
