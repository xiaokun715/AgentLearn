-- Prompt / Config 版本管理服务（设计说明书 §26~§29 数据库设计）
-- PostgreSQL 方言；docker-compose 首次启动会自动执行。
--
-- 核心思想：Immutable Version + Mutable Deployment
--   · prompt_versions / agent_configs 一旦创建不可修改（历史执行可复现）
--   · deployments 是可变的“路由表”，发布/灰度/回滚只改它
--   · audit_logs 记录每一次变更（Change Attribution）

CREATE TABLE prompts (
    id         UUID PRIMARY KEY,
    name       VARCHAR(128) UNIQUE NOT NULL,
    created_by VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE prompt_versions (
    id         UUID PRIMARY KEY,
    prompt_id  UUID NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    version    INT NOT NULL,
    template   TEXT NOT NULL,
    variables  JSONB,
    metadata   JSONB,
    created_by VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (prompt_id, version)
);

CREATE TABLE agent_configs (
    id         UUID PRIMARY KEY,
    agent_name VARCHAR(128) NOT NULL,
    version    INT NOT NULL,
    config     JSONB NOT NULL,
    created_by VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (agent_name, version)
);

CREATE TABLE deployments (
    id             UUID PRIMARY KEY,
    agent_name     VARCHAR(128) NOT NULL,
    environment    VARCHAR(32) NOT NULL,
    status         VARCHAR(32) NOT NULL,       -- STAGED | CANARY | RELEASED
    rules          JSONB NOT NULL,             -- [{"version":12,"weight":90},{"version":13,"weight":10}]
    experiment     VARCHAR(128),               -- A/B 实验名（可选）
    previous_rules JSONB,                      -- 回滚快照
    created_by     VARCHAR(128),
    created_at     TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL,
    UNIQUE (agent_name, environment)
);

CREATE TABLE audit_logs (
    id            BIGSERIAL PRIMARY KEY,
    actor         VARCHAR(128),
    action        VARCHAR(64),                 -- CREATE_PROMPT | DEPLOY | ROLLOUT | ROLLBACK ...
    resource_type VARCHAR(64),
    resource_id   VARCHAR(128),
    before        JSONB,
    after         JSONB,
    reason        TEXT,
    created_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_prompt_versions_prompt ON prompt_versions (prompt_id, version);
CREATE INDEX idx_agent_configs_agent   ON agent_configs (agent_name, version);
CREATE INDEX idx_deployments_env       ON deployments (agent_name, environment);
CREATE INDEX idx_audit_created         ON audit_logs (created_at DESC);
