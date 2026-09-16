# Mini Agent Harness

## —— 面向 AI Infra / Agent Platform 的生产级 Agent 调度基座

---

# 1. 项目定位

## 1.1 项目目标

实现一个轻量级、生产级思路的 Agent Runtime，重点验证以下 AI Infra 能力：

```text
Agent State Machine
        ↓
Checkpoint
        ↓
Tool Execution
        ↓
Async Job Queue
        ↓
Worker
        ↓
Lease / Heartbeat
        ↓
Idempotency
        ↓
Retry / DLQ
        ↓
Rate Limit / Circuit Breaker
        ↓
LLM Fallback
        ↓
Tracing
```

这个项目不追求实现一个复杂 Agent，而是重点实现：

> **如何让一个 Agent 在高并发、长任务、Worker 崩溃、LLM 故障、Tool 超时、重复执行等情况下仍然能够可靠运行。**

---

# 2. 为什么做这个 Demo

传统后端 Demo：

```text
HTTP
 ↓
Controller
 ↓
Service
 ↓
MySQL
```

对于 AI Infra 岗位价值有限。

本项目重点模拟：

```text
Agent
 ↓
LLM
 ↓
Tool
 ↓
Async Job
 ↓
Worker
 ↓
Checkpoint
 ↓
Recovery
```

因此 Redis、MQ、RPC、限流、熔断等技术都绑定在 Agent 的真实问题上。

---

# 3. 核心场景

设计一个：

# Agent Task Execution System

用户提交一个复杂任务：

```text
分析某个测试失败问题，
查询知识库，
执行测试，
分析日志，
生成最终诊断报告。
```

Agent 不一定一次完成。

可能：

```text
THINKING
   ↓
CALL_TOOL
   ↓
WAIT_TOOL
   ↓
THINKING
   ↓
CALL_TOOL
   ↓
WAIT_HUMAN
   ↓
THINKING
   ↓
FINISHED
```

因此它天然需要：

* 状态机
* Checkpoint
* 异步任务
* MQ
* Worker
* Tool 幂等
* Lease
* Heartbeat
* Retry
* DLQ
* Human-in-the-loop

---

# 4. 总体架构

```text
                           ┌──────────────┐
                           │    Client    │
                           └──────┬───────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │   API Gateway   │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │  Agent Service  │
                         │                 │
                         │ LangGraph/FSM   │
                         └────────┬────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    │             │             │
                    ▼             ▼             ▼
                LLM Gateway    Tool Gateway   Memory
                    │             │
                    │             ▼
                    │        Tool Scheduler
                    │             │
                    │       ┌─────┴─────┐
                    │       │           │
                    │       ▼           ▼
                    │     Sync        Async
                    │                   │
                    │                   ▼
                    │              Message Queue
                    │                   │
                    │          ┌────────┼────────┐
                    │          ▼        ▼        ▼
                    │       Worker1  Worker2  Worker3
                    │          │        │        │
                    │          └────────┼────────┘
                    │                   ▼
                    │               Sandbox
                    │
                    ▼
              LLM Providers
           ┌────────┼─────────┐
           ▼        ▼         ▼
        Primary  Backup1   Backup2


       ┌─────────────────────────────────────┐
       │             Infra Layer             │
       │                                     │
       │ Redis │ PostgreSQL │ MQ │ MinIO     │
       │ OTel  │ Prometheus │ Nacos          │
       └─────────────────────────────────────┘
```

---

# 5. 核心执行模型

Agent 定义为一个状态机：

```text
IDLE
 │
 ▼
THINKING
 │
 ├──────────────┐
 │              │
 ▼              ▼
ANSWER       CALL_TOOL
                │
                ▼
          EXECUTING_TOOL
                │
          ┌─────┴─────┐
          ▼           ▼
       SUCCESS       FAILED
          │           │
          │        RECOVERING
          │           │
          │      ┌────┼────┐
          │      ▼    ▼    ▼
          │    RETRY FALLBACK HUMAN
          │
          ▼
       THINKING
          │
          ▼
       FINISHED
```

另外：

```text
CALL_TOOL
   ↓
WAIT_TOOL
```

表示 Agent 不阻塞等待异步任务。

---

# 6. Agent State

核心状态：

```python
class AgentState(TypedDict):
    task_id: str
    session_id: str
    agent_id: str

    status: str
    step_index: int

    messages: list

    current_plan: list
    current_tool_call: dict | None

    tool_results: dict

    retry_count: int

    execution_path: list[str]

    error: dict | None
```

状态的核心原则：

> **Agent State 是可以被持久化和恢复的，而不是只存在 Worker 内存中。**

---

# 7. FSM 和 LangGraph 的关系

Demo 可以采用：

```text
LangGraph
```

实现 Agent Workflow。

概念上：

```text
LangGraph State
       ↓
Node
       ↓
Conditional Edge
       ↓
Next Node
```

例如：

```text
START
 ↓
THINK
 ↓
route()
 ├── answer → END
 ├── tool → TOOL
 └── human → HUMAN
```

LangGraph 负责：

* State
* Node
* Edge
* Conditional Routing
* Checkpoint
* Interrupt
* Resume

而底层基础设施由 Demo 自己实现：

```text
Tool Queue
Worker
Lease
Idempotency
Retry
DLQ
Rate Limit
Circuit Breaker
```

---

# 8. 项目目录

```text
mini-agent-harness/
│
├── apps/
│   ├── api/
│   │   └── main.py
│   ├── worker/
│   │   └── main.py
│   └── scheduler/
│       └── main.py
│
├── src/
│   │
│   ├── agent/
│   │   ├── graph.py
│   │   ├── state.py
│   │   ├── nodes.py
│   │   ├── router.py
│   │   └── recovery.py
│   │
│   ├── tool/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── gateway.py
│   │   ├── scheduler.py
│   │   └── executor.py
│   │
│   ├── queue/
│   │   ├── producer.py
│   │   ├── consumer.py
│   │   ├── retry.py
│   │   └── dlq.py
│   │
│   ├── lease/
│   │   ├── manager.py
│   │   ├── heartbeat.py
│   │   └── reaper.py
│   │
│   ├── idempotency/
│   │   ├── manager.py
│   │   └── lock.py
│   │
│   ├── checkpoint/
│   │   ├── store.py
│   │   └── recovery.py
│   │
│   ├── resilience/
│   │   ├── rate_limiter.py
│   │   ├── circuit_breaker.py
│   │   └── fallback.py
│   │
│   ├── llm/
│   │   ├── gateway.py
│   │   ├── provider.py
│   │   └── router.py
│   │
│   ├── sandbox/
│   │   ├── manager.py
│   │   └── policy.py
│   │
│   ├── storage/
│   │   ├── postgres.py
│   │   ├── redis.py
│   │   └── object_storage.py
│   │
│   └── observability/
│       ├── tracing.py
│       ├── metrics.py
│       └── logging.py
│
├── tools/
│   ├── calculator.py
│   ├── knowledge_search.py
│   ├── run_test.py
│   └── python_exec.py
│
├── configs/
│   ├── agent.yaml
│   ├── tools.yaml
│   └── resilience.yaml
│
├── tests/
│
├── docker-compose.yml
└── README.md
```

---

# 9. Redis 设计

Redis 在这个项目中不是简单的 Cache。

主要承担：

```text
1. Agent Runtime Hot State
2. Session State
3. Idempotency
4. Distributed Lock
5. Lease
6. Heartbeat
7. Rate Limiter
8. Circuit Breaker State
```

---

# 10. Agent Session

Redis Hash：

```text
agent:session:{session_id}
```

例如：

```text
status
current_task
step_index
last_heartbeat
worker_id
```

---

# 11. Tool Idempotency

```text
tool:idem:{idempotency_key}
```

例如：

```json
{
    "status": "PROCESSING",
    "call_id": "call_123",
    "worker_id": "worker_01",
    "lease_id": "lease_123"
}
```

状态：

```text
PROCESSING
SUCCESS
FAILED
```

---

# 12. 分布式锁

例如：

```text
tool:lock:{idempotency_key}
```

使用：

```text
SET key value NX EX 30
```

核心：

```text
if key not exists:
    acquire lock
    double check
    create PROCESSING
    execute
else:
    query status
```

锁不能作为业务状态本身。

应该区分：

```text
Lock
```

和：

```text
Idempotency State
```

---

# 13. Lease

执行 Tool：

```text
tool:lease:{call_id}
```

例如：

```text
TTL = 30s
Heartbeat = 10s
```

Worker：

```text
Acquire Lease
      ↓
Execute
      ↓
Heartbeat
      ↓
Heartbeat
      ↓
Heartbeat
```

如果：

```text
TTL expired
```

说明 Worker 可能已经失效。

---

# 14. Lease Reaper

后台 Reaper：

```text
每 5 秒
   ↓
扫描过期 Lease
   ↓
找到 PROCESSING Tool
   ↓
检查 Worker
   ↓
Recovery
```

恢复策略：

```text
Lease Expired
      │
      ▼
Check Tool Type
      │
 ┌────┴────┐
 ▼         ▼
Idempotent Non-idempotent
 ▼         ▼
Retry     Human/Query Status
```

---

# 15. Heartbeat

Worker：

```python
async def heartbeat(call_id, lease_id):
    while True:
        await lease_manager.renew(
            call_id,
            lease_id
        )
        await asyncio.sleep(10)
```

关键点：

> 续租必须验证 `lease_id`，防止旧 Worker 在任务已经被接管之后继续续租。

---

# 16. Agent Heartbeat

Agent Runtime 同样发送：

```text
agent:heartbeat:{run_id}
```

用途：

```text
判断 Agent 是否仍然存活
```

如果 Agent 消失：

```text
Agent Lease Expired
```

可以根据任务策略：

```text
继续后台 Tool
取消 Tool
等待 Agent 恢复
人工介入
```

---

# 17. Checkpoint

PostgreSQL：

```text
agent_checkpoint
```

字段：

```text
id
task_id
run_id
step_index
state_json
status
created_at
```

每一步：

```text
Node Execute
    ↓
Update State
    ↓
Checkpoint
    ↓
Next Node
```

---

# 18. Checkpoint 一致性

关键问题：

```text
Checkpoint 保存成功
但是下一步消息没发出去
```

或者：

```text
消息发送成功
但是 Checkpoint 没保存
```

因此：

```text
Checkpoint
+
Next Step Event
```

需要最终一致性。

推荐：

```text
PostgreSQL Transaction
        │
        ├── checkpoint
        └── outbox_event
                 │
                 ▼
             MQ Publisher
```

这就是：

# Outbox Pattern

---

# 19. MQ

Demo 推荐：

```text
RocketMQ
```

如果本地环境更简单：

```text
Kafka
```

或者第一阶段：

```text
Redis Streams
```

---

# 20. MQ Topic

```text
agent.task
tool.execute
tool.retry
tool.completed
tool.failed
agent.resume
agent.human
```

例如：

```text
tool.execute
```

消息：

```json
{
    "call_id": "call_123",
    "task_id": "task_001",
    "run_id": "run_001",
    "tool_name": "run_test",
    "idempotency_key": "idem_123"
}
```

---

# 21. 为什么 Agent 需要 MQ？

假设：

```text
1000 Agent
×
每个 Agent 需要执行一个 5 分钟 Tool
```

如果同步执行：

```text
1000 Request
 ↓
1000 Threads
 ↓
Memory / Connection / CPU
 ↓
OOM
```

加入 MQ：

```text
1000 Agent
    ↓
MQ
    ↓
Queue
    ↓
Worker Pool
```

Worker 可以控制并发：

```text
Worker = 20
```

于是：

```text
1000 tasks
    ↓
Queue Buffer
    ↓
20 Workers
```

---

# 22. Pull Consumer

Worker 不应该一次性取出所有任务。

采用：

```text
Pull
+
Bounded Concurrency
```

例如：

```text
max_inflight = 20
```

Worker：

```text
while True:

    if inflight >= 20:
        wait()

    message = queue.pull()

    execute(message)
```

---

# 23. MQ 顺序

同一个：

```text
session_id
```

或者：

```text
task_id
```

的 Agent Step：

```text
Step 1
Step 2
Step 3
```

原则上需要保证：

```text
Step 1
 ↓
Step 2
 ↓
Step 3
```

而不是：

```text
Step 3
 ↓
Step 1
 ↓
Step 2
```

可以使用：

```text
task_id / session_id
```

作为 Sharding Key。

---

# 24. 但为什么不能完全依赖 MQ 顺序？

因为即使 MQ 保证分区顺序：

```text
Worker Crash
Retry
Network Delay
```

仍然可能导致业务层状态异常。

因此最终还需要：

```text
step_index
```

进行状态校验：

```text
incoming_step == current_step + 1
```

否则：

```text
reject / delay / retry
```

---

# 25. Retry

失败：

```text
Tool
 ↓
FAILED
 ↓
Error Classifier
```

例如：

```text
Network Error
   ↓
Retry

Timeout
   ↓
Retry

429
   ↓
Backoff

Permission
   ↓
Stop

Invalid Parameter
   ↓
Repair

Non-idempotent Failure
   ↓
Human
```

---

# 26. Exponential Backoff

例如：

```text
attempt 1 → 1s
attempt 2 → 2s
attempt 3 → 4s
attempt 4 → 8s
```

增加：

```text
jitter
```

避免：

```text
大量 Worker
   ↓
同时 Retry
   ↓
下游再次被打爆
```

---

# 27. DLQ

超过最大 Retry：

```text
attempt >= 3
```

进入：

```text
Dead Letter Queue
```

例如：

```text
tool.retry
    ↓
retry #1
    ↓
retry #2
    ↓
retry #3
    ↓
DLQ
```

DLQ 不代表：

```text
任务失败结束
```

而是：

> **自动恢复失败，需要后续人工或者离线恢复。**

---

# 28. Tool Scheduler

Scheduler 根据 Tool Metadata 决定：

```text
SYNC
ASYNC
```

例如：

```text
calculator
    ↓
SYNC

run_test
    ↓
ASYNC
```

逻辑：

```python
if tool.estimated_duration_ms < threshold:
    sync_executor.execute()
else:
    async_executor.submit()
```

但不要只按照时间。

还应该考虑：

```text
resource requirement
risk
external dependency
concurrency
tool policy
```

---

# 29. Tool Registry

每个 Tool：

```yaml
run_test:
  execution_mode: async

  timeout: 300

  max_retry: 3

  idempotency: true

  permissions:
    - test.execute

  sandbox:
    cpu: 4
    memory: 4096
    network: false
```

---

# 30. Tool Gateway

所有 Tool 必须经过 Gateway：

```text
Agent
 ↓
Tool Gateway
 ↓
Schema Validation
 ↓
Parameter Repair
 ↓
Permission
 ↓
Injection Detection
 ↓
Idempotency
 ↓
Scheduler
```

禁止：

```text
Agent
 ↓
Python Function
```

直接执行。

---

# 31. Sandbox

执行：

```text
python_exec
run_test
compile
```

必须进入 Sandbox。

Demo：

```text
Docker
```

限制：

```text
CPU
Memory
Disk
Network
Process
Timeout
Filesystem
```

例如：

```yaml
sandbox:
  memory_mb: 2048
  cpu: 2
  timeout: 300
  network: false
```

---

# 32. Tool Result

Tool 结果分成：

```text
INLINE
ARTIFACT
```

例如：

```text
< 32 KB
```

直接返回。

超过：

```text
32 KB
```

保存：

```text
MinIO
```

返回：

```json
{
    "type": "artifact",
    "artifact_id": "artifact_001",
    "preview": "...",
    "size": 104857600
}
```

Agent 需要时：

```text
get_artifact
```

---

# 33. LLM Gateway

LLM 同样不能直接：

```text
Agent → OpenAI
```

应该：

```text
Agent
 ↓
LLM Gateway
 ↓
Rate Limit
 ↓
Circuit Breaker
 ↓
Load Balance
 ↓
Provider
```

Provider：

```text
Primary
Backup
Backup2
```

---

# 34. LLM 限流

可以按照：

```text
tenant
user
agent
model
provider
```

进行限流。

例如：

```text
tenant A
  ↓
100 RPM

agent B
  ↓
20 RPM
```

Token 维度也可以限制：

```text
tokens / minute
```

因为 LLM 的主要资源不是简单 Request 数量。

---

# 35. Token Bucket

Redis Lua 实现：

```text
bucket
├── capacity
├── tokens
└── last_refill
```

每次：

```text
Request
 ↓
Calculate Refill
 ↓
Check Tokens
 ↓
Consume
```

如果不足：

```text
429
```

---

# 36. Circuit Breaker

LLM：

```text
Primary Provider
```

连续：

```text
5 次 timeout
```

进入：

```text
OPEN
```

此时：

```text
Agent
 ↓
LLM Gateway
 ↓
Primary OPEN
 ↓
Backup Provider
```

经过：

```text
cooldown
```

进入：

```text
HALF_OPEN
```

探测成功：

```text
CLOSED
```

---

# 37. Circuit Breaker 状态

```text
             failure threshold
CLOSED ──────────────────────────► OPEN
  ▲                                  │
  │                                  │ cooldown
  │                                  ▼
  └──────────── success ───── HALF_OPEN
```

---

# 38. LLM Fallback

例如：

```text
Qwen-35B
   ↓ timeout
Qwen-27B
   ↓ unavailable
DeepSeek
   ↓ unavailable
Local Model
```

但不是无脑切换。

需要根据：

```text
capability
context length
tool calling
reasoning
cost
latency
```

选择。

---

# 39. Graceful Shutdown

Worker 收到：

```text
SIGTERM
```

不能立即：

```text
kill process
```

应该：

```text
SIGTERM
 ↓
STOP ACCEPTING NEW TASK
 ↓
Finish Current Tasks
 ↓
Checkpoint
 ↓
Release Lease
 ↓
Close MQ
 ↓
Close Redis
 ↓
Exit
```

如果任务无法完成：

```text
Checkpoint
+
Release Lease
```

让其他 Worker 接管。

---

# 40. 服务发现

Demo 可以加入：

```text
Nacos
```

服务：

```text
agent-service
tool-worker
llm-gateway
```

Worker 启动：

```text
Register
 ↓
Heartbeat
```

异常：

```text
Heartbeat lost
 ↓
Nacos mark unhealthy
 ↓
Gateway stop routing
```

---

# 41. RPC

Agent Service 与 Tool Worker 可以使用：

```text
gRPC
```

例如：

```text
Agent Service
      │
      │ gRPC
      ▼
Tool Worker
```

对于真正异步 Tool：

```text
MQ
```

负责任务派发。

因此：

```text
gRPC
→ 实时控制面

MQ
→ 异步任务数据面
```

这个区分非常适合面试。

---

# 42. Observability

整个 Agent Run：

```text
TraceID
```

例如：

```text
trace_001
```

下面：

```text
Span
├── agent.request
├── agent.think
├── tool.gateway
├── tool.validation
├── tool.queue
├── tool.execute
├── sandbox.execute
├── llm.request
└── checkpoint.save
```

这样可以看到：

```text
用户请求
 ↓
LLM
 ↓
Tool
 ↓
MQ
 ↓
Worker
```

整个链路。

---

# 43. Metrics

Agent：

```text
agent_task_total
agent_success_total
agent_failure_total
agent_recovery_total
agent_resume_total
```

Tool：

```text
tool_call_total
tool_success_total
tool_failure_total
tool_retry_total
tool_timeout_total
tool_duplicate_total
```

Queue：

```text
queue_depth
queue_wait_time
consumer_lag
```

Worker：

```text
worker_active_tasks
worker_failure
worker_lease_expired
```

LLM：

```text
llm_ttft
llm_latency
llm_tokens
llm_error
llm_fallback
llm_rate_limit
```

---

# 44. 核心故障实验

这个项目最重要的不是“正常流程跑通”。

而是：

> **主动杀进程，看系统能不能恢复。**

---

## Experiment 1：Agent Crash

```text
Agent
 ↓
Step 2
 ↓
Tool
 ↓
Checkpoint
 ↓
kill -9
```

恢复：

```text
Restart
 ↓
Load Checkpoint
 ↓
Pending Tool
 ↓
Idempotency
 ↓
SUCCESS / PROCESSING
 ↓
Resume
```

验证：

```text
Tool 不重复执行
```

---

# 45. Experiment 2：Worker Crash

```text
Worker A
 ↓
PROCESSING
 ↓
kill -9
```

然后：

```text
Lease Expire
 ↓
Reaper
 ↓
Worker B
 ↓
Recovery
```

验证：

```text
任务最终继续执行
```

---

# 46. Experiment 3：双 Worker 并发

同时：

```text
Worker A
Worker B
```

消费：

```text
same idempotency_key
```

验证：

```text
只有一个 Worker 获得执行权
```

---

# 47. Experiment 4：Tool Timeout

```text
Tool
 ↓
sleep(600)
```

配置：

```text
timeout = 30s
```

验证：

```text
Sandbox Kill
 ↓
FAILED
 ↓
Retry
```

---

# 48. Experiment 5：LLM 故障

模拟：

```text
Primary Provider
 ↓
HTTP 500
```

验证：

```text
Failure threshold
 ↓
Circuit OPEN
 ↓
Fallback Provider
```

---

# 49. Experiment 6：MQ 堆积

模拟：

```text
1000 tasks
```

Worker：

```text
10
```

观察：

```text
Queue Depth
Consumer Lag
Worker Active
```

验证：

```text
API 不 OOM
```

---

# 50. Experiment 7：参数错误

Agent：

```json
{
    "timeout": "300",
    "test_cases": "TC001"
}
```

Tool Schema：

```text
timeout: int
test_cases: list[str]
```

执行：

```text
Schema Error
 ↓
Parameter Repair
 ↓
Validation
 ↓
Execute
```

---

# 51. Experiment 8：DAG Loop

Agent：

```text
search
 ↓
analyze
 ↓
search
 ↓
analyze
```

检测：

```text
execution_path
```

超过：

```text
threshold = 3
```

执行：

```text
DEGRADED
```

继续失败：

```text
STOP
```

---

# 52. Experiment 9：Human-in-the-loop

模拟：

```text
database.delete
```

Risk：

```text
HIGH
```

状态：

```text
WAIT_HUMAN
```

前端：

```text
Approve
Reject
Modify
```

Approve：

```text
Checkpoint Resume
 ↓
Execute
```

---

# 53. 关键数据库

最终至少需要：

```text
agent_task
agent_checkpoint
tool_execution
tool_result
execution_event
outbox_event
```

---

## agent_task

```text
task_id
session_id
agent_id
status
current_step
created_at
updated_at
```

---

## agent_checkpoint

```text
id
task_id
run_id
step_index
state_json
created_at
```

---

## tool_execution

```text
call_id
task_id
run_id
tool_name
idempotency_key
arguments
status
attempt
worker_id
lease_id
error_type
result_id
created_at
updated_at
```

---

# 54. 核心状态机

Agent：

```text
IDLE
 ↓
THINKING
 ↓
CALL_TOOL
 ↓
WAIT_TOOL
 ↓
THINKING
 ↓
FINISHED
```

Tool：

```text
CREATED
 ↓
VALIDATING
 ↓
QUEUED
 ↓
PROCESSING
 ↓
SUCCESS
```

异常：

```text
PROCESSING
 ↓
FAILED
 ↓
RECOVERING
 ├── RETRY
 ├── FALLBACK
 ├── HUMAN
 └── ABORT
```

---

# 55. 完整 Agent 执行链

```text
                         User
                           │
                           ▼
                    Agent API
                           │
                           ▼
                    LangGraph
                           │
                           ▼
                     THINKING
                           │
                           ▼
                    Tool Gateway
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
       Validation      Permission     Security
            │              │              │
            └──────────────┼──────────────┘
                           ▼
                     Idempotency
                           │
                           ▼
                       Scheduler
                           │
                  ┌────────┴────────┐
                  ▼                 ▼
                Sync              Async
                  │                 │
                  │                 ▼
                  │                MQ
                  │                 │
                  │          ┌──────┼──────┐
                  │          ▼      ▼      ▼
                  │        W1      W2     W3
                  │          │      │      │
                  └──────────┼──────┼──────┘
                             ▼
                          Lease
                             │
                             ▼
                          Sandbox
                             │
                             ▼
                           Tool
                             │
                       ┌─────┴─────┐
                       ▼           ▼
                    SUCCESS      ERROR
                       │           │
                       │       Classifier
                       │           │
                       │     ┌─────┼─────┐
                       │     ▼     ▼     ▼
                       │   Retry Fallback Human
                       │
                       ▼
                  Persist Result
                       │
                       ▼
                   Checkpoint
                       │
                       ▼
                 Resume Graph
                       │
                       ▼
                    THINKING
                       │
                       ▼
                    FINISHED
```

---

# 56. 最终技术栈

推荐第一版：

```text
Language
└── Python

Agent
└── LangGraph

API
└── FastAPI

Database
└── PostgreSQL

State / Lock / Lease
└── Redis

Message Queue
└── RocketMQ

Artifact
└── MinIO

Sandbox
└── Docker

RPC
└── gRPC

Service Discovery
└── Nacos

Observability
├── OpenTelemetry
└── Prometheus

LLM
├── OpenAI Compatible
└── Local Qwen/vLLM
```

---

# 57. 推荐的最小可运行版本

如果一次实现全部组件，工程量会比较大。

第一版只需要：

```text
FastAPI
+
LangGraph
+
Redis
+
PostgreSQL
+
Redis Streams
+
Docker
```

完成：

```text
Agent
 ↓
LangGraph
 ↓
Tool
 ↓
Redis Queue
 ↓
Worker
 ↓
Lease
 ↓
Heartbeat
 ↓
Checkpoint
 ↓
Recovery
```

跑通以后再替换：

```text
Redis Streams
```

为：

```text
RocketMQ
```

再加入：

```text
gRPC
Nacos
Prometheus
OTel
```

这样学习效率最高。

---

# 58. 最终项目应该展示的 Demo

README 最好直接放下面这张流程图：

```text
User
 │
 ▼
Agent
 │
 ▼
LangGraph
 │
 ├───────────────┐
 │               │
 ▼               ▼
LLM             Tool
 │               │
 │               ▼
 │          Idempotency
 │               │
 │               ▼
 │          Async Queue
 │               │
 │          ┌────┴────┐
 │          ▼         ▼
 │       Worker A   Worker B
 │          │         │
 │          └────┬────┘
 │               ▼
 │             Lease
 │               │
 │               ▼
 │            Sandbox
 │               │
 │               ▼
 │             Tool
 │               │
 │        ┌──────┴──────┐
 │        ▼             ▼
 │     Success        Failure
 │                       │
 │                  Retry/Fallback
 │                       │
 └───────────────┬───────┘
                 ▼
             Checkpoint
                 │
                 ▼
             Resume Agent
```

然后提供一个脚本：

```bash
python examples/kill_worker.py
```

演示：

```text
Worker A
   ↓
执行 Tool
   ↓
kill -9
   ↓
Lease Expired
   ↓
Worker B
   ↓
Recovery
   ↓
Tool 完成
   ↓
Agent Resume
```

这比单纯展示：

```text
POST /task
GET /task
```

更能体现这个项目的价值。

---

# 59. 这个项目对应的面试知识树

```text
Mini Agent Harness
│
├── Agent Runtime
│   ├── FSM
│   ├── LangGraph
│   ├── State
│   └── Checkpoint
│
├── Redis
│   ├── Hash
│   ├── TTL
│   ├── Distributed Lock
│   ├── Lease
│   ├── Heartbeat
│   └── Lua
│
├── MQ
│   ├── Producer
│   ├── Consumer
│   ├── Pull
│   ├── Backpressure
│   ├── Retry
│   ├── DLQ
│   ├── Ordering
│   └── Transaction / Outbox
│
├── Distributed System
│   ├── Idempotency
│   ├── Consistency
│   ├── Leader / Worker
│   ├── Failover
│   └── Graceful Shutdown
│
├── Tool Runtime
│   ├── Sync
│   ├── Async
│   ├── Sandbox
│   ├── Timeout
│   └── Resource Limit
│
├── LLM Infra
│   ├── Gateway
│   ├── Rate Limit
│   ├── Circuit Breaker
│   ├── Fallback
│   └── Load Balance
│
└── Observability
    ├── Trace
    ├── Span
    ├── Metrics
    └── Logging
```

---

# 60. 面试时项目的核心表述

这个项目不要描述成：

> “我做了一个 Agent Demo。”

应该描述成：

> **我实现了一个 Mini Agent Harness，主要解决 Agent 长任务执行过程中的可靠性问题。Agent 使用 LangGraph 管理状态和 Checkpoint，Tool 调用经过统一 Tool Gateway，根据 Tool 的执行特征选择同步或异步执行。对于异步长任务，我使用 MQ 解耦 Agent 和 Worker，并通过 Redis 实现 Idempotency、分布式锁、Lease 和 Heartbeat。**
>
> **Agent 每完成一个步骤都会持久化 Checkpoint。如果 Agent 或 Worker 崩溃，可以根据 Checkpoint 和 Tool Idempotency 状态恢复，而不会简单地重新执行所有步骤。Tool 执行放在 Sandbox 中，同时有 Timeout、资源限制、参数校验和权限控制。对于失败任务，通过 Error Classifier 选择 Retry、Backoff、Fallback、DLQ 或 Human Intervention。**
>
> **在 LLM 层又增加 Gateway、Rate Limit、Circuit Breaker 和 Provider Fallback，并通过 OpenTelemetry 将 Agent → LLM → Tool → Worker 的完整链路串起来。**

最终把这个项目浓缩成一句话：

> **“我不是在实现一个 Agent，而是在实现 Agent 的运行时基础设施。”**

---

# 61. 项目最终验收标准

这个 Demo 做完以后，至少应该能够证明下面 10 件事：

```text
□ 1. 一个 Agent 可以执行多步骤任务

□ 2. Agent 每一步都有 Checkpoint

□ 3. 长任务可以通过 MQ 异步执行

□ 4. Worker 有 Lease + Heartbeat

□ 5. kill -9 Worker 后任务可以被接管

□ 6. kill -9 Agent 后可以从 Checkpoint Resume

□ 7. 相同 Idempotency Key 不会重复产生 Tool 副作用

□ 8. Tool Timeout 可以自动 Recovery

□ 9. LLM Primary 故障可以 Circuit Break + Fallback

□ 10. 完整请求可以通过 TraceID 查看 Agent → LLM → Tool → Worker 链路
```

完成这 10 项之后，再增加：

```text
Parameter Self-Healing
Sandbox
DAG Cycle Detection
Human-in-the-loop
Nacos
gRPC
RocketMQ
```

就会从一个普通的 Agent Demo，逐步变成一个比较完整的 **Mini Agent Runtime / AI Infra 实战项目**。
