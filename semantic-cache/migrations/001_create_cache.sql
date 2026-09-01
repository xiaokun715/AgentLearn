-- 001_create_cache.sql
-- Semantic Cache 核心表（设计说明书 §29 ~ §31）
--
-- 需要 PostgreSQL 安装 pgvector 扩展：
--   CREATE EXTENSION IF NOT EXISTS vector;
--
-- 表字段与 §29 保持一致，额外增加：
--   namespace         —— 缓存域隔离（普通 chat / agent 子任务等）
--   fingerprint       —— 精确缓存 key（§9），system prompt/model/temperature/版本全部参与
--   system_fingerprint —— 用于语义命中后的 Safety 二次校验（§18 / §8）
--   temperature       —— 用于 Safety 校验（§19）
--   agent_type        —— Agent Cache Scope（§38）
--   task_type         —— Agent Cache Scope（§38）

CREATE TABLE IF NOT EXISTS semantic_cache (
    id                 UUID PRIMARY KEY,
    namespace          VARCHAR(128) NOT NULL DEFAULT 'semantic-cache',
    tenant_id          VARCHAR(128) NOT NULL,
    model              VARCHAR(128) NOT NULL,
    fingerprint        VARCHAR(64)  NOT NULL,
    system_fingerprint VARCHAR(64),
    prompt             TEXT         NOT NULL,
    embedding          vector(1024),
    response           JSONB        NOT NULL,
    temperature        DOUBLE PRECISION NOT NULL DEFAULT 0,
    knowledge_version  VARCHAR(128),
    agent_type         VARCHAR(128),
    task_type          VARCHAR(128),
    created_at         TIMESTAMP    NOT NULL,
    expires_at         TIMESTAMP    NOT NULL,
    hit_count          BIGINT       NOT NULL DEFAULT 0
);

-- 精确缓存 O(1) 索引：fingerprint 已被 tenant/model/namespace 过滤
CREATE INDEX IF NOT EXISTS semantic_cache_fp_idx
    ON semantic_cache (namespace, tenant_id, model, fingerprint);

-- 向量索引：HNSW + cosine（§30）
CREATE INDEX IF NOT EXISTS semantic_cache_embedding_idx
    ON semantic_cache USING hnsw (embedding vector_cosine_ops);
