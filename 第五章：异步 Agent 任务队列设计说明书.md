# 06. Async Agent Job Queue

## 1. 项目定位

项目名称：

```text
async-agent-job-queue
```

目标：

> 实现一个简化版的、支持持久化、重试、Checkpoint、取消和死信处理的 Agent 异步任务执行平台。

最终用户调用：

```http
POST /v1/jobs
```

只负责：

```text
创建任务
    ↓
立即返回 job_id
```

而不是：

```text
HTTP Request
    ↓
Agent 执行 5 分钟
    ↓
HTTP Response
```

---

# 2. 最终 Demo 效果

用户：

```http
POST /v1/jobs
```

请求：

```json
{
  "agent": "research_agent",
  "input": {
    "query": "分析 NVIDIA 最新财报"
  }
}
```

立即返回：

```json
{
  "job_id": "job_123",
  "status": "queued"
}
```

然后后台：

```text
                 Job API
                    │
                    ▼
              Job Database
                    │
                    ▼
                Message Queue
                    │
                    ▼
              ┌────────────┐
              │  Worker 1  │
              ├────────────┤
              │  Worker 2  │
              ├────────────┤
              │  Worker 3  │
              └────────────┘
                    │
                    ▼
                 Agent
                    │
             ┌──────┼──────┐
             ▼      ▼      ▼
           Tool    LLM    Tool
             │      │      │
             └──────┼──────┘
                    ▼
               Checkpoint
                    │
                    ▼
                Job State
```

用户可以随时：

```http
GET /v1/jobs/job_123
```

得到：

```json
{
  "job_id": "job_123",
  "status": "running",
  "current_step": "research",
  "progress": 60
}
```

---

# 3. 为什么不能直接 HTTP 调 Agent？

这是这个项目首先需要理解的问题。

传统 API：

```text
Client
  │
  │ HTTP
  ▼
Server
  │
  │
  │ execute()
  │
  ▼
Response
```

假设 Agent：

```text
LLM → Search → LLM → Tool → LLM → Report
```

执行 3 分钟。

HTTP 连接可能发生：

```text
Client timeout
Gateway timeout
Load Balancer timeout
Worker restart
Network disconnect
```

于是：

```text
HTTP connection lost
       ↓
Agent task lost
```

问题就在于：

> **HTTP Request 生命周期和 Agent Job 生命周期不应该绑定。**

---

# 4. 正确架构

应该拆成：

```text
                    ┌─────────────┐
Client ────────────►│   Job API   │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │ Job Store   │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │ MessageQueue│
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
           Worker 1     Worker 2     Worker 3
              │
              ▼
          Agent Runtime
              │
              ▼
         Checkpoint Store
```

HTTP 只负责：

```text
Create
Get
Cancel
```

Worker 负责：

```text
Execute
Retry
Checkpoint
```

---

# 5. 核心概念：Job

首先定义：

```python
@dataclass
class Job:

    id: str

    agent_name: str

    input: dict

    status: str

    priority: int

    retry_count: int

    max_retries: int

    current_step: str | None

    created_at: float

    updated_at: float

    error: str | None
```

状态：

```text
QUEUED
  │
  ▼
RUNNING
  │
  ├──────────────► SUCCESS
  │
  ├──────────────► FAILED
  │
  ├──────────────► RETRYING
  │
  └──────────────► CANCELLED

FAILED
  │
  ▼
DLQ
```

---

# 6. Job State Machine

建议把它画成整个项目最核心的一张图：

```text
                   ┌────────────┐
                   │   QUEUED   │
                   └─────┬──────┘
                         │
                         ▼
                   ┌────────────┐
                   │  RUNNING   │
                   └─────┬──────┘
                         │
             ┌───────────┼───────────┐
             │           │           │
             ▼           ▼           ▼
          SUCCESS      RETRY       CANCEL
                         │
                         ▼
                    RETRYING
                         │
                         ▼
                      RUNNING
                         │
                         ▼
                  max retries?
                    │       │
                   NO      YES
                    │       │
                    │       ▼
                    │      DLQ
                    │
                    └──────►
```

---

# 7. 为什么一定需要状态机？

不能简单：

```python
job.status = "running"
```

然后到处修改。

因为实际运行可能出现：

```text
Worker A:
RUNNING

Worker A:
crash

Worker B:
接管任务

Worker B:
继续执行
```

如果状态没有严格定义，很容易出现：

```text
RUNNING
SUCCESS
RETRYING
```

状态互相覆盖。

因此建议实现：

```python
class JobStatus(str, Enum):

    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD = "dead"
```

然后集中管理状态迁移。

---

# 8. Job API

提供：

```text
POST   /v1/jobs
GET    /v1/jobs/{job_id}
POST   /v1/jobs/{job_id}/cancel
GET    /v1/jobs/{job_id}/events
```

---

## 创建 Job

```http
POST /v1/jobs
```

Request：

```json
{
  "agent": "research_agent",
  "input": {
    "query": "分析 NVIDIA 最新财报"
  }
}
```

Response：

```json
{
  "job_id": "job_001",
  "status": "queued"
}
```

关键：

> **这里绝对不能等待 Agent 执行完成。**

---

# 9. Query Job

```http
GET /v1/jobs/job_001
```

返回：

```json
{
  "job_id": "job_001",
  "status": "running",
  "current_step": "search",
  "progress": 40,
  "retry_count": 0
}
```

成功：

```json
{
  "job_id": "job_001",
  "status": "success",
  "result": {
    "report": "..."
  }
}
```

---

# 10. Cancel Job

```http
POST /v1/jobs/job_001/cancel
```

状态：

```text
QUEUED
   ↓
CANCELLED
```

如果已经：

```text
RUNNING
```

则需要：

```text
Cancellation Signal
        ↓
Worker
        ↓
Agent
        ↓
停止执行
```

注意：

> Cancel 不是简单地把数据库 status 改成 cancelled。

否则：

```text
DB:
CANCELLED

Worker:
继续调用 LLM
继续执行 Tool
```

最终状态就不一致。

---

# 11. Queue

Demo 第一版：

```python
asyncio.Queue
```

例如：

```python
queue = asyncio.Queue()
```

Producer：

```python
await queue.put(job_id)
```

Consumer：

```python
job_id = await queue.get()
```

Worker：

```python
while True:

    job_id = await queue.get()

    try:
        await execute(job_id)
    finally:
        queue.task_done()
```

---

# 12. 但是 asyncio.Queue 有一个致命问题

如果：

```text
Worker
 ↓
asyncio.Queue
 ↓
process crash
```

队列里面的数据：

```text
全部丢失
```

因此：

```text
asyncio.Queue
```

只适合：

> Demo 第一阶段理解 Queue 机制。

真正项目：

```text
Redis Streams
RabbitMQ
Kafka
SQS
```

或者：

```text
PostgreSQL Job Queue
```

---

# 13. 推荐 Demo 演进路线

不要一步到位。

### V1

```text
FastAPI
+
asyncio.Queue
+
SQLite/PostgreSQL
```

学习：

```text
Job
Worker
State
```

### V2

```text
Redis
+
Redis Streams
```

学习：

```text
Message Delivery
Ack
Consumer Group
```

### V3

```text
PostgreSQL
+
Redis
```

实现：

```text
Job Store
+
Queue
+
Checkpoint
```

---

# 14. Worker Pool

例如：

```text
Worker Pool

Worker-1
Worker-2
Worker-3
Worker-4
```

代码：

```python
async def worker(worker_id):

    while True:

        job_id = await queue.get()

        try:
            await execute_job(
                worker_id,
                job_id
            )

        finally:
            queue.task_done()
```

启动：

```python
workers = [
    asyncio.create_task(worker(i))
    for i in range(4)
]
```

---

# 15. Worker 不应该直接执行 Job

更合理：

```text
Worker
   ↓
JobExecutor
   ↓
AgentRuntime
   ↓
Agent
```

结构：

```text
Worker
 │
 ▼
JobExecutor
 │
 ├── load_job()
 ├── acquire_lease()
 ├── resume_checkpoint()
 ├── execute_agent()
 ├── checkpoint()
 ├── retry()
 └── finalize()
```

这样 Worker 只负责：

> **消费任务。**

而 JobExecutor 负责：

> **任务生命周期。**

---

# 16. Agent Runtime

Demo 可以实现一个简单 Agent：

```text
ResearchAgent

Step 1:
Analyze Query

Step 2:
Search

Step 3:
Analyze Result

Step 4:
Generate Report
```

```python
class ResearchAgent:

    async def run(self, state):

        state = await self.analyze(state)

        state = await self.search(state)

        state = await self.analyze_result(state)

        state = await self.generate_report(state)

        return state
```

---

# 17. 为什么需要 Checkpoint？

假设：

```text
Step 1 ✓
Step 2 ✓
Step 3 ✓
Step 4 ✗
```

如果没有 Checkpoint：

```text
重新执行：

Step 1
Step 2
Step 3
Step 4
```

浪费：

```text
LLM tokens
Tool calls
时间
```

有 Checkpoint：

```text
Checkpoint:

Step 1 ✓
Step 2 ✓
Step 3 ✓

Worker Crash

      ↓

Resume

      ↓

Step 4
```

---

# 18. Checkpoint 是什么？

Checkpoint 本质上是：

> **Agent 在某个安全恢复点的完整执行状态。**

例如：

```json
{
  "job_id": "job_001",
  "step": "search",
  "state": {
    "query": "NVIDIA revenue",
    "search_result": [
      "..."
    ]
  },
  "completed_steps": [
    "analyze",
    "search"
  ]
}
```

---

# 19. Checkpoint Store

定义：

```python
class CheckpointStore:

    async def save(
        self,
        job_id: str,
        checkpoint: dict
    ):
        ...

    async def load(
        self,
        job_id: str
    ):
        ...

    async def delete(
        self,
        job_id: str
    ):
        ...
```

---

# 20. Checkpoint 的粒度

这是 Agent 系统非常重要的设计问题。

最简单：

```text
每个 Node 完成后 checkpoint
```

例如：

```text
Node A
 ↓
Checkpoint
 ↓
Node B
 ↓
Checkpoint
 ↓
Node C
 ↓
Checkpoint
```

这和你之前研究 LangGraph Checkpointer 的思路是一致的。

---

# 21. 为什么 Tool 执行后也应该 Checkpoint？

例如：

```text
Agent
 ↓
Call Search API
 ↓
返回 100 条结果
 ↓
Worker Crash
```

如果没有保存：

```text
Search API
```

恢复时又调用：

```text
Search API
```

可能：

* 浪费钱
* 数据发生变化
* Tool 有副作用
* 得到不同结果

因此推荐：

```text
Tool Call
 ↓
Tool Result
 ↓
Checkpoint
```

---

# 22. Tool Idempotency

这里会出现一个非常重要的问题。

假设 Agent：

```text
调用：
create_order()
```

Tool 已经成功：

```text
订单创建成功
```

但是：

```text
Worker 在 checkpoint 前崩溃
```

恢复后：

```text
重新调用 create_order()
```

结果：

```text
创建两个订单
```

所以：

> **Checkpoint + Retry 必须考虑副作用和幂等性。**

---

# 23. Tool Execution Record

可以记录：

```json
{
  "tool_call_id": "call_123",
  "tool": "create_order",
  "arguments": {
    "user_id": "u1"
  },
  "status": "success",
  "result": {
    "order_id": "o123"
  }
}
```

恢复时：

```text
tool_call_id = call_123
```

如果已经：

```text
SUCCESS
```

就不要再次执行。

---

# 24. Retry Policy

不是所有异常都应该 Retry。

例如：

```text
LLM timeout
→ Retry

HTTP 500
→ Retry

Rate Limit
→ Retry + Backoff

Invalid Prompt
→ 不 Retry

Permission Denied
→ 不 Retry
```

因此：

```python
class RetryPolicy:

    def should_retry(
        self,
        error
    ) -> bool:
        ...
```

---

# 25. Exponential Backoff

例如：

```text
Retry 1:
1 sec

Retry 2:
2 sec

Retry 3:
4 sec

Retry 4:
8 sec
```

公式：

```text
delay = base * 2^retry_count
```

最好加：

```text
jitter
```

例如：

```text
delay = min(
    max_delay,
    base * 2 ** retry_count
)

delay += random.uniform(
    0,
    0.5
)
```

避免：

```text
1000 个 Worker
同时 Retry
```

造成：

> Retry Storm。

---

# 26. Retry 次数

例如：

```text
max_retries = 3
```

执行：

```text
Attempt 1
   ↓
FAILED
   ↓
Attempt 2
   ↓
FAILED
   ↓
Attempt 3
   ↓
FAILED
   ↓
DLQ
```

---

# 27. Dead Letter Queue

DLQ：

> Dead Letter Queue

用于存放：

```text
无法成功执行
+
已经超过 Retry 次数
```

例如：

```text
Job
 ↓
Attempt 1 FAIL
 ↓
Attempt 2 FAIL
 ↓
Attempt 3 FAIL
 ↓
DLQ
```

不要：

```text
无限 Retry
```

否则：

```text
坏任务
 ↓
Queue
 ↓
Worker
 ↓
FAIL
 ↓
Queue
 ↓
Worker
 ↓
FAIL
 ↓
...
```

最终：

> 把整个系统拖垮。

---

# 28. DLQ 中保存什么？

```json
{
  "job_id": "job_001",
  "reason": "max_retries_exceeded",
  "retry_count": 3,
  "last_error": "LLM timeout",
  "failed_at": "2026-09-01T20:00:00"
}
```

然后管理员可以：

```text
查看
人工修复
重新入队
```

例如：

```http
POST /v1/dlq/job_001/retry
```

---

# 29. Job Lease

这是 Worker 系统必须理解的另一个核心概念。

假设：

```text
Worker A
   ↓
Job 001
```

A 崩溃。

数据库仍然：

```text
status = RUNNING
```

那么：

```text
Worker B
```

不知道能不能接管。

因此 Job 需要：

```text
lease_owner
lease_expire_at
```

例如：

```json
{
  "status": "running",
  "worker_id": "worker-1",
  "lease_expire_at": "20:05:00"
}
```

---

# 30. Lease 机制

Worker：

```text
获取 Job
 ↓
Acquire Lease
 ↓
执行
 ↓
不断 Heartbeat
```

例如：

```text
lease = 30 seconds
```

Worker 每：

```text
10 seconds
```

刷新：

```text
lease_expire_at
```

如果 Worker Crash：

```text
Heartbeat 消失
 ↓
30 seconds
 ↓
Lease Expired
```

其他 Worker：

```text
重新 Claim Job
```

---

# 31. 这样就形成故障恢复

```text
Worker A
   │
   ▼
Job 001
   │
   ▼
Checkpoint 3
   │
   ▼
Worker A Crash
   │
   X
   │
Lease Expired
   │
   ▼
Worker B
   │
   ▼
Load Checkpoint 3
   │
   ▼
Continue Step 4
```

这就是：

> **断点续跑。**

---

# 32. Job Database

推荐 PostgreSQL。

表：

```sql
CREATE TABLE jobs (
    id UUID PRIMARY KEY,

    tenant_id VARCHAR(128) NOT NULL,

    agent_name VARCHAR(128) NOT NULL,

    input JSONB NOT NULL,

    status VARCHAR(32) NOT NULL,

    priority INT DEFAULT 0,

    retry_count INT DEFAULT 0,

    max_retries INT DEFAULT 3,

    current_step VARCHAR(128),

    worker_id VARCHAR(128),

    lease_expire_at TIMESTAMP,

    result JSONB,

    error TEXT,

    created_at TIMESTAMP NOT NULL,

    updated_at TIMESTAMP NOT NULL
);
```

---

# 33. Checkpoint Table

```sql
CREATE TABLE checkpoints (
    id UUID PRIMARY KEY,

    job_id UUID NOT NULL,

    step VARCHAR(128) NOT NULL,

    state JSONB NOT NULL,

    created_at TIMESTAMP NOT NULL,

    UNIQUE(job_id, step)
);
```

---

# 34. Event Store

强烈建议再增加：

```sql
CREATE TABLE job_events (
    id BIGSERIAL PRIMARY KEY,

    job_id UUID NOT NULL,

    event_type VARCHAR(64) NOT NULL,

    payload JSONB,

    created_at TIMESTAMP NOT NULL
);
```

例如：

```text
JOB_CREATED
JOB_STARTED
STEP_STARTED
TOOL_CALLED
TOOL_COMPLETED
CHECKPOINT_SAVED
STEP_FAILED
JOB_RETRYING
JOB_COMPLETED
JOB_CANCELLED
JOB_DEAD
```

这样你可以实现：

```text
Job State
+
Event History
```

---

# 35. 为什么同时保存 State 和 Event？

这是一个非常值得面试的时候讲的问题。

State：

```text
当前状态是什么？
```

Event：

```text
为什么变成这个状态？
```

例如：

```text
Job:

status = RUNNING
current_step = search
```

Event：

```text
JOB_CREATED
↓
JOB_STARTED
↓
STEP_STARTED analyze
↓
STEP_COMPLETED analyze
↓
STEP_STARTED search
```

所以：

```text
State = Snapshot
Event = History
```

---

# 36. Event Store 还能支持什么？

例如：

```http
GET /v1/jobs/job_001/events
```

返回：

```json
[
  {
    "type": "JOB_CREATED"
  },
  {
    "type": "STEP_COMPLETED",
    "step": "analyze"
  },
  {
    "type": "TOOL_COMPLETED",
    "tool": "search"
  }
]
```

这就开始具备：

> **Agent 可观测性 / 审计能力。**

也可以和你之前研究的：

```text
LangSmith
OpenTelemetry
Metrics
Structured Logging
```

连接起来。

---

# 37. 推荐的完整架构

```text
                         Client
                           │
                           ▼
                    ┌─────────────┐
                    │   Job API   │
                    └──────┬──────┘
                           │
               ┌───────────┴───────────┐
               ▼                       ▼
        ┌─────────────┐        ┌─────────────┐
        │  Job Store  │        │ Event Store │
        └──────┬──────┘        └─────────────┘
               │
               ▼
        ┌─────────────┐
        │    Queue    │
        └──────┬──────┘
               │
       ┌───────┼────────┐
       ▼       ▼        ▼
   Worker 1 Worker 2 Worker 3
       │
       ▼
 ┌───────────────┐
 │ Job Executor  │
 └───────┬───────┘
         │
         ▼
 ┌─────────────────┐
 │  Agent Runtime  │
 └───────┬─────────┘
         │
    ┌────┼────┐
    ▼    ▼    ▼
   LLM  Tool  LLM
         │
         ▼
 ┌─────────────────┐
 │ CheckpointStore │
 └─────────────────┘
```

旁边再有：

```text
RetryPolicy
DLQ
Metrics
Tracing
```

---

# 38. 项目目录

我建议直接按照这个结构：

```text
async-agent-job-queue/
│
├── README.md
├── pyproject.toml
├── docker-compose.yml
│
├── app/
│   │
│   ├── api/
│   │   ├── jobs.py
│   │   └── dlq.py
│   │
│   ├── domain/
│   │   ├── job.py
│   │   ├── status.py
│   │   └── events.py
│   │
│   ├── queue/
│   │   ├── base.py
│   │   ├── memory.py
│   │   └── redis.py
│   │
│   ├── worker/
│   │   ├── worker.py
│   │   └── pool.py
│   │
│   ├── executor/
│   │   └── job_executor.py
│   │
│   ├── agent/
│   │   ├── base.py
│   │   └── research_agent.py
│   │
│   ├── checkpoint/
│   │   ├── base.py
│   │   └── postgres.py
│   │
│   ├── retry/
│   │   └── policy.py
│   │
│   ├── dlq/
│   │   └── manager.py
│   │
│   ├── storage/
│   │   ├── job_store.py
│   │   └── event_store.py
│   │
│   └── observability/
│       ├── metrics.py
│       └── tracing.py
│
├── migrations/
│   ├── 001_jobs.sql
│   ├── 002_checkpoints.sql
│   └── 003_events.sql
│
├── tests/
│   ├── test_job.py
│   ├── test_worker.py
│   ├── test_retry.py
│   ├── test_checkpoint.py
│   ├── test_recovery.py
│   ├── test_cancel.py
│   ├── test_dlq.py
│   └── test_idempotency.py
│
└── examples/
    ├── basic_job.py
    ├── failure_recovery.py
    └── agent_job.py
```

---

# 39. 核心执行流程

最终 `JobExecutor` 大概应该是：

```python
async def execute(job_id):

    job = await job_store.get(job_id)

    await acquire_lease(job)

    checkpoint = await checkpoint_store.load(
        job_id
    )

    state = checkpoint or create_initial_state(job)

    try:

        while not state.finished:

            step = state.next_step()

            await event_store.append(
                job_id,
                "STEP_STARTED",
                {"step": step}
            )

            result = await execute_step(
                step,
                state
            )

            state = state.apply(result)

            await checkpoint_store.save(
                job_id,
                state
            )

            await event_store.append(
                job_id,
                "CHECKPOINT_SAVED",
                {"step": step}
            )

        await job_store.success(
            job_id,
            state.result
        )

    except RetryableError as e:

        await retry_manager.retry(
            job,
            e
        )

    except Exception as e:

        await job_store.fail(
            job_id,
            str(e)
        )
```

---

# 40. 这里有一个非常关键的问题

不要写成：

```python
execute_step()

checkpoint()
```

然后认为：

```text
万事大吉
```

因为存在：

```text
Tool Execution
      ↓
成功
      ↓
Worker Crash
      ↓
Checkpoint 没保存
```

于是恢复：

```text
重新 Tool Execution
```

所以真正的 Agent Runtime 要考虑：

```text
Execution
+
Durability
+
Idempotency
```

这也是你后面学习：

```text
LangGraph
Temporal
Durable Execution
```

时最重要的基础。

---

# 41. 故障注入

这个 Demo **一定要做故障注入**。

否则你只是写了一个普通 Queue。

例如：

```python
if step == "analyze":
    raise WorkerCrash()
```

测试：

```text
Step 1 ✓
Step 2 ✓
Step 3 ✗
```

然后：

```text
Worker 1 Crash
        ↓
Lease Expired
        ↓
Worker 2
        ↓
Load Checkpoint
        ↓
Continue Step 3
```

这才真正证明：

> Checkpoint + Worker Recovery 工作了。

---

# 42. Retry 测试

模拟：

```text
LLM timeout
```

第一次：

```text
FAILED
```

第二次：

```text
FAILED
```

第三次：

```text
SUCCESS
```

验证：

```text
retry_count = 2
```

并观察：

```text
1s
2s
4s
```

Backoff。

---

# 43. DLQ 测试

模拟：

```text
Tool 永久失败
```

执行：

```text
Attempt 1
 ↓
FAIL

Attempt 2
 ↓
FAIL

Attempt 3
 ↓
FAIL

DLQ
```

然后：

```http
POST /v1/dlq/job_001/retry
```

重新进入：

```text
Queue
```

---

# 44. Cancel 测试

创建：

```text
Job
 ↓
Queue
 ↓
Worker
 ↓
RUNNING
```

调用：

```http
POST /jobs/{id}/cancel
```

验证：

```text
Agent
 ↓
收到 cancellation
 ↓
停止 LLM / Tool
 ↓
Checkpoint
 ↓
CANCELLED
```

---

# 45. 并发测试

例如：

```text
100 jobs
10 workers
```

观察：

```text
Queue depth
Active workers
Job latency
Success rate
Retry rate
```

然后：

```text
1000 jobs
10 workers
```

看：

```text
Queue backlog
```

这就开始进入：

> **Agent Runtime 的吞吐与容量规划。**

---

# 46. 必做 Metrics

至少：

```text
agent_jobs_created_total

agent_jobs_completed_total

agent_jobs_failed_total

agent_jobs_cancelled_total

agent_jobs_retried_total

agent_jobs_dead_total

agent_job_duration_seconds

agent_job_queue_wait_seconds

agent_job_execution_seconds

agent_checkpoint_total

agent_checkpoint_failure_total

agent_queue_depth

agent_worker_active
```

其中特别关注：

```text
Queue Wait Time
```

和：

```text
Execution Time
```

因为：

```text
Job Total Latency
=
Queue Wait
+
Execution
```

---

# 47. Agent 系统中一个非常重要的指标

例如：

```text
Job:

Queue Wait = 20s
Execution = 60s

Total = 80s
```

如果你只看：

```text
Agent Execution = 60s
```

会误认为 Agent 很慢。

其实：

```text
20s
```

是队列拥塞。

所以：

```text
Queue Latency
```

和：

```text
Agent Latency
```

必须分开统计。

---

# 48. 多租户

既然你之前的 Gateway / Semantic Cache 都考虑了 Tenant，那么这个项目也应该保留：

```text
tenant_id
```

例如：

```text
Tenant A
  ├── Job 1
  ├── Job 2
  └── Job 3

Tenant B
  ├── Job 4
  └── Job 5
```

队列层还可以进一步做：

```text
Per-Tenant Queue
```

防止：

```text
Tenant A
提交 10000 个 Job
```

导致：

```text
Tenant B
完全没有资源。
```

---

# 49. 最后可以加入 Fair Scheduling

例如：

```text
Tenant A:
1000 jobs

Tenant B:
10 jobs
```

如果简单 FIFO：

```text
A A A A A A A A A ...
```

B 会饿死。

可以实现：

```text
Round Robin
```

或者：

```text
Weighted Fair Queue
```

例如：

```text
Tenant A weight = 10
Tenant B weight = 1
```

这一步已经开始进入：

> **LLM / Agent 基础设施调度系统。**

---

# 50. 与 LangGraph 的关系

你之前已经研究过 LangGraph Checkpointer。

这里可以把关系理解得非常清楚：

```text
Job Queue
    │
    │
    ▼
Agent Runtime
    │
    ▼
LangGraph
    │
    ├── Node
    ├── Edge
    ├── State
    └── Checkpoint
```

所以：

> **Job Queue 解决“任务生命周期”，LangGraph Checkpointer 解决“Agent 图内部执行状态”。**

两者不是一回事。

例如：

```text
Job
 │
 ├── status = RUNNING
 │
 ├── worker_id = worker-2
 │
 └── retry_count = 1
          │
          ▼
     LangGraph
          │
          ├── current_node = fault_diagnosis
          ├── messages
          ├── tool_results
          └── state
```

---

# 51. 与 Temporal 的关系

你给出的 Temporal 也可以这样理解：

```text
你自己实现：

Job Queue
+
Worker
+
Retry
+
Timer
+
Checkpoint
+
Recovery
+
State
+
Event
```

实际上已经在造一个非常简化的：

```text
Durable Execution Engine
```

Temporal 则把这些基础设施做成了成熟的平台。

所以学习路径应该是：

```text
手写 Demo
   ↓
理解 Queue
   ↓
理解 Worker
   ↓
理解 Checkpoint
   ↓
理解 Retry
   ↓
理解 Lease
   ↓
理解 Idempotency
   ↓
理解 Durable Execution
   ↓
Temporal
```

---

# 52. 与你前面 Semantic Cache 的结合

现在可以开始组合你的项目。

例如 Agent：

```text
Job
 ↓
Worker
 ↓
Agent
 ↓
LLM
```

加入：

```text
Semantic Cache
```

变成：

```text
Job
 ↓
Worker
 ↓
Agent
 ↓
Semantic Cache
 │
 ├── HIT → Result
 │
 └── MISS
       ↓
      LLM
       ↓
     Cache
```

再加入：

```text
Token Meter
```

最终：

```text
Job
 ↓
Worker
 ↓
Agent
 ↓
Semantic Cache
 ↓
LLM Gateway
 ├── RateLimiter
 ├── TokenMeter
 └── StreamInfra
```

你前面的 01～06 项目就真正开始形成一个系统了。

---

# 53. 整个学习路线现在变成

```text
LLM Infrastructure
│
├── 01 LLM Gateway
│      └── Router / Provider / API
│
├── 02 Token Meter & Billing
│      └── Token / Cost / Stripe
│
├── 03 Stream Infrastructure
│      └── SSE / Backpressure / Resume
│
├── 04 Semantic Cache
│      └── Embedding / Vector / TTL
│
├── 05 Rate Limiter
│      └── Token Bucket / Distributed Limit
│
└── 06 Async Agent Job Queue
       │
       ├── Job
       ├── Queue
       ├── Worker
       ├── State Machine
       ├── Checkpoint
       ├── Retry
       ├── Lease
       ├── DLQ
       ├── Event Store
       └── Durable Execution
```

---

# 54. 这个 Demo 的分阶段实现

我建议你**不要一次实现所有功能**，按照下面 5 个 Phase 做。

### Phase 1：最小 Job Queue

```text
FastAPI
+
asyncio.Queue
+
Worker
+
PostgreSQL
```

实现：

```text
POST /jobs
GET /jobs/{id}
Worker
Job State
```

---

### Phase 2：Checkpoint

加入：

```text
Checkpoint Store
```

实现：

```text
Step 1
 ↓
Checkpoint
 ↓
Step 2
 ↓
Checkpoint
```

然后模拟：

```text
Worker Crash
```

验证恢复。

---

### Phase 3：Retry + DLQ

加入：

```text
RetryPolicy
Exponential Backoff
DLQ
```

实现：

```text
FAIL
 ↓
Retry
 ↓
Retry
 ↓
Retry
 ↓
DLQ
```

---

### Phase 4：生产级 Worker

加入：

```text
Redis Streams
Worker Pool
Lease
Heartbeat
Idempotency
Cancellation
```

这一阶段才真正开始像基础设施。

---

### Phase 5：Agent Runtime

最后接：

```text
LangGraph
```

或者自己实现：

```text
AgentState
Node
Tool
Checkpoint
Resume
```

最终实现：

```text
Job
 ↓
Worker
 ↓
LangGraph Agent
 ↓
Node A
 ↓
Checkpoint
 ↓
Node B
 ↓
Tool
 ↓
Checkpoint
 ↓
Node C
 ↓
SUCCESS
```

---

## 55. 最后你需要真正搞懂的 10 个问题

这个项目做完后，不要只满足于“代码跑起来”，你应该能够解释：

1. **为什么 Agent 不能直接用同步 HTTP 执行？**
2. **Queue 和 Job Database 为什么需要同时存在？**
3. **Worker Crash 后，谁负责发现任务需要恢复？**
4. **Checkpoint 应该保存什么？**
5. **为什么 Tool 执行后必须考虑 Checkpoint？**
6. **Retry 为什么必须区分可重试和不可重试异常？**
7. **为什么需要 Exponential Backoff + Jitter？**
8. **为什么需要 Lease / Heartbeat？**
9. **为什么 Retry + Checkpoint 还需要 Idempotency？**
10. **Job Queue、Checkpoint、LangGraph Checkpointer、Temporal Durable Execution 分别解决什么问题？**

其中第 **8、9、10** 个尤其重要。

如果把整个项目浓缩成一句面试答案，可以说：

> **我会把长时间运行的 Agent 从 HTTP 请求生命周期中解耦出来，将 Agent 任务抽象成持久化 Job，通过 Queue + Worker Pool 异步执行；执行过程中以 Node/Tool 为粒度保存 Checkpoint，并通过 Lease/Heartbeat 检测 Worker 故障、通过 Retry Policy 和 Exponential Backoff 处理瞬时错误，超过重试次数进入 DLQ；同时利用 Event Store 保存完整生命周期事件，使任务具备可恢复、可审计和可观测能力。最终再将这一套 Job Runtime 与 LangGraph Checkpointer 结合，实现 Agent 级别的 Durable Execution。**

这就是这个第 06 个 Demo 最核心的知识闭环。
