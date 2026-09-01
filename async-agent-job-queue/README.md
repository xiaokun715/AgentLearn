# Async Agent Job Queue

> 一个简化版、支持**持久化、重试、Checkpoint、取消和死信处理**的 Agent 异步任务执行平台。
> 把长时间运行的 Agent 从 HTTP 请求生命周期中解耦出来，抽象成持久化 Job，通过 Queue + Worker Pool 异步执行。

对应设计说明书：`../第五章：异步 Agent 任务队列设计说明书.md`

---

## 1. 为什么不能直接用同步 HTTP 调 Agent？

假设 Agent 要跑 3 分钟：

```text
LLM → Search → LLM → Tool → LLM → Report
```

HTTP 连接可能随时断掉（客户端超时、网关超时、负载均衡超时、Worker 重启、网络抖动）——
一旦连接断了，正在执行的 Agent 任务就丢了。

> **核心结论：HTTP Request 生命周期 和 Agent Job 生命周期不应该绑定。**

## 2. 正确架构

```text
                    ┌─────────────┐
Client ────────────►│   Job API   │   POST /v1/jobs → { job_id, status: "queued" }  立即返回
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │ Job Store   │   持久化 Job 状态
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │ MessageQueue│   进程崩溃也不丢消息（生产用 Redis Streams / PG）
                    └──────┬──────┘
              ┌────────────┼────────────┐
              ▼            ▼            ▼
           Worker 1     Worker 2     Worker 3
              │
              ▼
          Agent Runtime      ← ResearchAgent / chaos_agent
              │
              ▼
         Checkpoint Store    ← 每个 step / Tool 后保存
```

- **HTTP 只负责**：Create / Get / Cancel（立即返回）
- **Worker 负责**：Execute / Retry / Checkpoint

## 3. 快速开始

零依赖即可运行（默认 SQLite + asyncio 内存队列）：

```bash
pip install -e ".[test]"

# 启动 HTTP API（Worker Pool + Reaper 随应用一起启动）
uvicorn app.main:app --port 8000

# 提交一个任务（立即返回）
curl -X POST http://localhost:8000/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{"agent":"research_agent","input":{"query":"分析 NVIDIA 最新财报"}}'
# → {"job_id":"job_xxx","status":"queued"}

# 查询状态
curl http://localhost:8000/v1/jobs/job_xxx
# → {"job_id":"job_xxx","status":"running","current_step":"search","progress":40,...}

# 取消
curl -X POST http://localhost:8000/v1/jobs/job_xxx/cancel

# 事件历史（State=快照，Event=历史）
curl http://localhost:8000/v1/jobs/job_xxx/events

# 指标（Prometheus 文本格式）
curl http://localhost:8000/metrics

# 死信队列
curl http://localhost:8000/v1/dlq
curl -X POST http://localhost:8000/v1/dlq/job_xxx/retry
```

跑示例脚本：

```bash
python examples/basic_job.py          # 基本生命周期 + 事件 + 指标
python examples/failure_recovery.py   # 崩溃恢复 + 断点续跑 + Tool 幂等
python examples/agent_job.py          # 逐步 Checkpoint + Tool 事件
```

跑测试：

```bash
python -m pytest tests/ -q
```

## 4. 目录结构

```
async-agent-job-queue/
├── app/
│   ├── api/            # jobs.py / dlq.py / schemas.py
│   ├── domain/         # job / status / events / state_machine / exceptions
│   ├── queue/          # base / memory / redis(Streams)
│   ├── worker/         # worker / pool / reaper
│   ├── executor/       # job_executor.py（任务生命周期）
│   ├── agent/          # base / state / tools / llm / research_agent / chaos_agent / registry
│   ├── checkpoint/     # base / memory / sqlite / postgres
│   ├── retry/          # policy.py（可重试/不可重试 + 指数退避 + jitter）
│   ├── dlq/            # manager.py
│   ├── storage/        # job_store / event_store / memory / sqlite / postgres
│   ├── observability/  # metrics.py / tracing.py
│   ├── factory.py      # 应用装配
│   ├── service.py      # JobService / DlqService
│   └── main.py         # FastAPI 入口
├── migrations/         # 001_jobs / 002_checkpoints / 003_events（PostgreSQL 方言）
├── examples/
└── tests/              # 8+ 个测试文件
```

## 5. 核心概念

### Job 状态机（§6-7）

```text
QUEUED ──► RUNNING ──┬──► SUCCESS
         │           ├──► RETRYING ──► QUEUED/RUNNING
         │           ├──► FAILED ──► DEAD
         ▼           └──► CANCELLED
      CANCELLED
```

状态只能通过 `JobStateMachine` + 存储层「条件 UPDATE」迁移，
杜绝两个 Worker 并发时 RUNNING / SUCCESS / RETRYING 互相覆盖。

### Lease / Heartbeat（§29-31）

Worker 获取 Job 时原子 `acquire_lease`，执行期间每 N 秒 `heartbeat` 续约。
Worker 崩溃 → Heartbeat 消失 → 租约过期 → **Reaper** 扫描到过期租约 → 重新入队
→ 其他 Worker 从 Checkpoint 断点续跑。

### Checkpoint（§17-21）

每个 step 完成后保存安全恢复点：`completed_steps + tool_records`。
Tool 调用做了 **write-ahead**：先落 `running` 记录再执行，成功后落 `success` 记录
—— 这解决了 §40 最危险的窗口（Tool 已成功但 step Checkpoint 未保存）。

### Retry Policy + Backoff（§24-25）

- `RetryableError`（LLM timeout / 5xx / rate limit）→ 重试
- `NonRetryableError`（Invalid Prompt / 权限不足）→ 不重试，直接 FAILED
- 延迟 = `min(max_delay, base * 2**retry_count) + jitter`，防 Retry Storm

### DLQ（§26-28）

超过 `max_retries` → 进入 DLQ（status=DEAD），管理员可 `POST /v1/dlq/{id}/retry` 人工重投。

## 6. API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/jobs` | 创建任务，立即返回 `{job_id, status}` |
| GET | `/v1/jobs/{job_id}` | 查询状态 / 进度 / 结果 |
| POST | `/v1/jobs/{job_id}/cancel` | 取消（QUEUED 直接取消；RUNNING 发协作式信号） |
| GET | `/v1/jobs/{job_id}/events` | 事件历史（JOB_CREATED/STEP_STARTED/TOOL_CALLED/...） |
| GET | `/v1/dlq` | 死信列表 |
| POST | `/v1/dlq/{job_id}/retry` | 从 DLQ 重新入队 |
| GET | `/metrics` | Prometheus 指标 |
| GET | `/healthz` | 健康检查 |

可用 Agent：`research_agent`（标准流程）、`chaos_agent`（故障注入，见 examples/failure_recovery.py）。

## 7. 后端切换

默认零依赖。可选 PostgreSQL / Redis（`docker compose up -d` 后切换）：

```bash
# PostgreSQL 存储 + Redis Streams 队列
STORAGE_BACKEND=postgres DATABASE_URL="postgresql://jobq:jobq@localhost:5432/jobq" \
QUEUE_BACKEND=redis REDIS_URL="redis://localhost:6379/0" \
uvicorn app.main:app --port 8000
```

注意：Redis Streams 队列 + 单进程 Demo 里，`get` 用 Consumer Group 实现「至少一次 + Ack」；
SQLite / PostgreSQL 存储则负责 Job 状态与 Checkpoint 的持久化。

## 8. 指标（§46）

必做指标均落在 `/metrics`：

```text
agent_jobs_created_total / completed / failed / cancelled / retried / dead
agent_job_duration_seconds / queue_wait_seconds / execution_seconds
agent_checkpoint_total / checkpoint_failure_total
agent_queue_depth / worker_active
```

> **特别关注：Queue Wait 与 Execution 必须分开统计**（§47）。
> Job Total Latency = Queue Wait + Execution。只看 Execution 会把「队列拥塞」误判成「Agent 很慢」。

## 9. 你真正需要搞懂的 10 个问题

1. **为什么 Agent 不能直接用同步 HTTP 执行？**
   HTTP 连接会超时/断开，而 Agent 要跑几分钟；连接断 ≠ 任务该丢。
   把 Job 生命周期与请求生命周期解耦，让任务在系统内部独立存活。

2. **Queue 和 Job Database 为什么需要同时存在？**
   一个管「待办顺序」，一个管「状态真相」。Queue 决定谁先被消费，DB 决定恢复点、
   进度、结果；两者互补，缺一不可（Queue 丢了可以从 DB 恢复，DB 丢了一切归零）。

3. **Worker Crash 后，谁负责发现任务需要恢复？**
   **Reaper（清扫器）**。它定期扫描 RUNNING/RETRYING 且租约过期的 Job，
   重置回 QUEUED 并重新入队，其他 Worker 接管后从 Checkpoint 续跑。

4. **Checkpoint 应该保存什么？**
   能支撑「断点续跑」的最小完整状态：completed_steps（已完成的步骤）、
   current_step、agent 状态字段、以及 Tool 执行记录（幂等依据）。

5. **为什么 Tool 执行后必须考虑 Checkpoint？**
   Tool 调用花钱/有副作用/结果会变。若 Tool 成功但 Checkpoint 没保存，
   恢复后会重复调用 —— 浪费钱、产生脏数据、得到不同结果。

6. **Retry 为什么必须区分可重试和不可重试异常？**
   瞬时错误（timeout/5xx/限流）重试有意义；配置错误（Invalid Prompt）重试一万次
   还是失败，只会浪费资源。区分后，「不 Retry」本身就是一种正确策略。

7. **为什么需要 Exponential Backoff + Jitter？**
   固定频率重试会造成「Retry Storm」：1000 个 Worker 同时打爆下游。
   指数退避让重试间隔放大，Jitter 让同批任务错峰，避免同步的浪涌。

8. **为什么需要 Lease / Heartbeat？**
   DB 里的 RUNNING 不能证明 Worker 还活着。Lease 是「我有权处理」的凭证，
   Heartbeat 是「我还活着」的证据；租约过期 = 可以安全接管。否则会并发双跑。

9. **为什么 Retry + Checkpoint 还需要 Idempotency？**
   Retry 意味着同一段代码可能执行多次；Checkpoint 覆盖不到「Tool 成功但未落盘」的窗口。
   幂等（Tool Execution Record + write-ahead + 确定性 tool_call_id）保证重放时
   已成功的副作用不会重复发生。

10. **Job Queue、Checkpoint、LangGraph Checkpointer、Temporal Durable Execution 分别解决什么问题？**
    - Job Queue：任务生命周期（创建/排队/取消/重试/DLQ）
    - Checkpoint：执行过程中的安全恢复点（粒度 = step/tool）
    - LangGraph Checkpointer：Agent 图内部执行状态（messages / node / state）
    - Temporal：把上述全部做成生产级 Durable Execution 平台
    - **两者不是一回事**：Job Queue 管「任务生命周期」，LangGraph Checkpointer 管「Agent 图内部状态」；
    本项目 = 手写一个极简的 Durable Execution Engine，为理解 Temporal 打基础。

> **一句话面试答案**：
> 我会把长时间运行的 Agent 从 HTTP 请求生命周期中解耦出来，将 Agent 任务抽象成持久化 Job，
> 通过 Queue + Worker Pool 异步执行；执行过程中以 Node/Tool 为粒度保存 Checkpoint，
> 并通过 Lease/Heartbeat 检测 Worker 故障、通过 Retry Policy 和 Exponential Backoff 处理瞬时错误，
> 超过重试次数进入 DLQ；同时利用 Event Store 保存完整生命周期事件，
> 使任务具备可恢复、可审计和可观测能力。最终再将这一套 Job Runtime 与 LangGraph Checkpointer
> 结合，实现 Agent 级别的 Durable Execution。

## 10. 设计要点对照（设计说明书章节）

| 设计说明书 | 实现 |
|---|---|
| §7 状态机集中管理 | `domain/state_machine.py` + 存储层条件 UPDATE |
| §10 协作式取消 | `cancel_requested` 信号 + Worker 在执行边界感知 |
| §13 V1 演进路线 | 默认 SQLite + asyncio.Queue，可选 PG/Redis |
| §15 Worker/Executor 分层 | `worker/` 只消费，`executor/` 管生命周期 |
| §19-21 Checkpoint | `checkpoint/` 每 step 保存 `completed_steps + tool_records` |
| §22-23 Tool 幂等 | write-ahead + 确定性 `tool_call_id` + TOOL_SKIPPED 事件 |
| §25 Backoff+Jitter | `retry/policy.py::compute_backoff` |
| §28 DLQ | `dlq/manager.py` + `POST /v1/dlq/{id}/retry` |
| §29-31 Lease/Heartbeat/Reaper | `worker/reaper.py` + JobStore 原子租约 |
| §34-36 Event Store | `job_events` 表 + `/events` 接口 |
| §41 故障注入 | `chaos_agent`（crash_after_tool / fail_at / fail_with） |
| §46-47 指标 | `observability/metrics.py` + `/metrics` |

## 11. 与你前面项目的组合（§52）

```text
Job → Worker → Agent → Semantic Cache → LLM Gateway(RateLimiter/TokenMeter/StreamInfra)
```

本 Demo 的 `ctx.llm()` 就是接入语义缓存与 LLM 网关的位置。
