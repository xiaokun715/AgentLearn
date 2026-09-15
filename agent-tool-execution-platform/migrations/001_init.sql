-- =====================================================================
-- 可靠工具执行平台 —— 初始 Schema（说明书 §42）
--
-- 角色：PostgreSQL = Durable Source of Truth（§44）
--       Redis      = 高性能协调层（幂等键 / 租约 / 心跳 / 队列）
--
-- 本文件是**生产用**的 PostgreSQL DDL。Demo 默认跑在 SQLite 上
-- （`app/infra/database.py` 内嵌等价 schema，见其中的 `SCHEMA`），
-- 差异只有类型与自增语法，表结构、索引、约束意图完全一致。
--
-- 三张表的分工：
--   tool_execution   执行快照与状态机落点（§41）
--   tool_result      结果本体或 artifact 引用（§37~§40）
--   execution_event  审计 / 排障 / 恢复 / 可观测性的事件流（§42）
--   outbox_event     §46 的 Outbox，保证 DB 提交后再驱动 Redis
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 执行记录
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_execution (
    id                BIGSERIAL PRIMARY KEY,
    call_id           TEXT        NOT NULL UNIQUE,
    run_id            TEXT        NOT NULL DEFAULT '',
    agent_id          TEXT        NOT NULL DEFAULT '',
    tenant_id         TEXT        NOT NULL DEFAULT 'tenantA',

    tool_name         TEXT        NOT NULL,
    tool_version      TEXT        NOT NULL DEFAULT '1.0',

    -- §7 稳定的幂等键。**必须带唯一性约束**：
    -- 幂等性的最后一道防线是数据库，不是 Redis ——
    -- 即使 Redis 全丢，这条唯一索引也能保证同一个逻辑操作只有一行执行记录。
    idempotency_key   TEXT        NOT NULL,

    -- 原始参数与参数指纹（§30 重复检测用 arguments_hash）
    arguments         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    arguments_hash    TEXT        NOT NULL DEFAULT '',

    -- §41 状态机：CREATED/VALIDATING/QUEUED/ACQUIRING/PROCESSING/
    --             SUCCESS/FAILED/RECOVERING/RECOVERY_REQUIRED/
    --             WAITING_HUMAN/CANCELLING/CANCELLED/COMPLETED
    status            TEXT        NOT NULL DEFAULT 'CREATED',
    attempt           INTEGER     NOT NULL DEFAULT 0,

    -- §14 租约归属：Worker 崩溃后靠这两个字段判断「谁在跑、能不能接管」
    worker_id         TEXT,
    lease_id          TEXT,

    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,

    -- §34 错误分类结果，不是自由文本
    error_type        TEXT,
    error_message     TEXT,

    result_id         TEXT,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT tool_execution_status_valid CHECK (status IN (
        'CREATED','VALIDATING','QUEUED','ACQUIRING','PROCESSING',
        'SUCCESS','FAILED','RECOVERING','RECOVERY_REQUIRED',
        'WAITING_HUMAN','CANCELLING','CANCELLED','COMPLETED'
    ))
);

CREATE INDEX IF NOT EXISTS idx_exec_idem   ON tool_execution(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_exec_run    ON tool_execution(run_id);
CREATE INDEX IF NOT EXISTS idx_exec_status ON tool_execution(status);

-- 幂等键唯一：同一个逻辑操作只允许一行。
-- 注意用 UNIQUE 而不是「查询后再插入」——后者在并发下有竞态窗口。
CREATE UNIQUE INDEX IF NOT EXISTS uq_exec_idempotency
    ON tool_execution(idempotency_key);

-- 租约回收扫描：找回所有 PROCESSING 且长时间没更新的记录（§56）
CREATE INDEX IF NOT EXISTS idx_exec_lease_scan
    ON tool_execution(status, updated_at)
    WHERE status IN ('PROCESSING','ACQUIRING');

-- ---------------------------------------------------------------------
-- 结果（§37~§40）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_result (
    id             BIGSERIAL PRIMARY KEY,
    call_id        TEXT        NOT NULL,

    -- inline：小结果，直接进 Agent Context（§38）
    -- artifact：大结果，只留对象存储引用 + preview（§39）
    result_type    TEXT        NOT NULL DEFAULT 'inline'
                               CHECK (result_type IN ('inline','artifact')),

    inline_result  TEXT,
    artifact_id    TEXT,

    size_bytes     BIGINT      NOT NULL DEFAULT 0,
    content_hash   TEXT        NOT NULL DEFAULT '',

    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 两种形态必须二选一，不能都不填（否则 Agent 拿到空结果还不知道为什么）
    CONSTRAINT tool_result_payload_present CHECK (
        (result_type = 'inline'   AND inline_result IS NOT NULL) OR
        (result_type = 'artifact' AND artifact_id  IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_result_call ON tool_result(call_id);

-- ---------------------------------------------------------------------
-- 事件流（§42）—— audit / debug / recovery / observability
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_event (
    id          BIGSERIAL PRIMARY KEY,
    call_id     TEXT        NOT NULL,
    event_type  TEXT        NOT NULL,
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_event_call ON execution_event(call_id, id);
CREATE INDEX IF NOT EXISTS idx_event_type ON execution_event(event_type, created_at DESC);

-- ---------------------------------------------------------------------
-- Outbox（§46）
--
-- 为什么需要它：结果落库与 Redis 状态更新是两个系统，没有分布式事务。
-- 做法是把「要通知 Redis 什么」和业务数据**放在同一个本地事务**里提交，
-- 之后由投递器（或崩溃重启后的补偿任务）把 published=0 的事件补发出去。
-- 于是不会出现「Redis 说 SUCCESS、PostgreSQL 还说 PROCESSING」的撕裂（§45）。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox_event (
    id            BIGSERIAL PRIMARY KEY,
    aggregate_id  TEXT        NOT NULL,
    event_type    TEXT        NOT NULL,
    payload       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    published     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at  TIMESTAMPTZ
);

-- 投递器只关心未投递的，用部分索引避免全表扫描
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
    ON outbox_event(id)
    WHERE published = FALSE;

COMMIT;

-- =====================================================================
-- Redis 侧的数据结构（不在本文件，仅作对照，§43）
--
--   idempotency:{key}          -> HASH/STRING  §8 状态机（PROCESSING/SUCCESS/FAILED）
--   lease:{call_id}            -> STRING       §14 租约（worker_id / lease_id / expire_at）
--   lease:expirations          -> ZSET         §56 Reaper 的过期区间扫描索引
--   agent_heartbeat:{agent}:{run} -> HASH      §16 Agent 心跳
--   tool_lock:{key}            -> STRING       §9 分布式锁（value 存 owner，释放时校验）
--   tool_status:{call_id}      -> STRING       执行状态快照
--   approval:{call_id}         -> STRING       §52 人工审批单
--   tool_jobs                  -> STREAM       §4.2 异步任务队列（消费者组 workers）
--   tool_jobs:delayed          -> ZSET         §36 退避重试的延迟队列
-- =====================================================================
