# 01. 项目定位

项目名称：

```text
agent-event-fanout/
```

目标架构：

```text
                    Agent / Job
                         │
                         │ publish event
                         ▼
                ┌─────────────────┐
                │  Event Ingress  │
                └────────┬────────┘
                         │
                         ▼
                ┌─────────────────┐
                │   Event Store   │
                │   PostgreSQL    │
                └────────┬────────┘
                         │
                         ▼
                ┌─────────────────┐
                │   Event Queue   │
                │      Redis      │
                └────────┬────────┘
                         │
                 ┌───────┼────────┐
                 ▼       ▼        ▼
              Worker   Worker   Worker
                 │       │        │
                 ▼       ▼        ▼
              CRM     Ticket    Slack
```

其中每个 Worker 实际负责：

```text
Event
 ↓
查 Subscriber
 ↓
生成签名
 ↓
HTTP POST
 ↓
判断成功/失败
 ↓
Retry / Success / DLQ
```

---

# 02. 你到底要解决什么问题？

假设 Agent 完成一个任务：

```json
{
  "job_id": "job_123",
  "status": "completed"
}
```

现在有三个客户系统：

```text
CRM
工单系统
Slack
```

你不能：

```python
requests.post(crm)
requests.post(ticket)
requests.post(slack)
```

因为如果：

```text
CRM 请求成功
Ticket 请求超时
Slack 请求失败
```

你的 Agent 到底应该怎么办？

而且：

```text
Agent
 ↓
Webhook
 ↓
客户服务器 500
 ↓
重试
```

如果重试导致客户收到两次：

```text
ticket_created
ticket_created
```

又怎么办？

所以需要解决：

```text
可靠投递
+
至少一次投递
+
幂等
+
重试
+
DLQ
+
签名
+
Fan-out
```

---

# 03. 第一原则：Event 和 Delivery 必须分离

这是整个项目最重要的设计。

不要：

```text
Event
 ↓
HTTP POST
 ↓
失败
```

而应该：

```text
              Event
                │
                ▼
          Event Store
                │
                ▼
          Fan-out Queue
                │
       ┌────────┼────────┐
       ▼        ▼        ▼
    Delivery  Delivery  Delivery
      #1        #2        #3
       │        │        │
      CRM     Ticket    Slack
```

也就是说：

> **Event 表示“发生了什么”，Delivery 表示“这个事件发送给谁、发送到什么状态”。**

这是非常重要的解耦。

---

# 04. Event

例如：

```json
{
  "id": "evt_001",

  "type": "agent.job.completed",

  "created_at": "2026-09-02T01:00:00Z",

  "tenant_id": "tenant_001",

  "data": {
    "job_id": "job_123",
    "status": "completed",
    "result": {
      "answer": "..."
    }
  }
}
```

Event 一旦创建：

> **Immutable**

不能修改。

---

# 05. Event Type

建议定义：

```text
agent.job.created
agent.job.running
agent.job.completed
agent.job.failed

agent.tool.started
agent.tool.completed
agent.tool.failed

agent.workflow.completed
agent.workflow.failed
```

以后可以扩展：

```text
knowledge.document.ingested
knowledge.document.failed

fine_tuning.started
fine_tuning.completed
fine_tuning.failed
```

---

# 06. Subscriber

客户系统首先需要注册：

```json
{
  "id": "sub_001",

  "tenant_id": "tenant_001",

  "url": "https://customer.com/webhooks/agent",

  "events": [
    "agent.job.completed",
    "agent.job.failed"
  ],

  "secret": "..."
}
```

所以：

```text
Subscriber
=
谁订阅
+
订阅什么
+
发到哪里
+
怎么验证
```

---

# 07. Subscriber Registry

数据库：

```text
subscribers
```

字段：

```text
id
tenant_id
url
secret
status
created_at
```

再单独建立：

```text
subscriber_events
```

例如：

```text
subscriber_id
event_type
```

这样：

```text
tenant_001
    │
    ├── CRM
    │     ├── job.completed
    │     └── job.failed
    │
    └── Ticket
          └── job.completed
```

---

# 08. Event Fan-out

假设：

```text
Event #001

type:
agent.job.completed
```

有：

```text
Subscriber A → CRM
Subscriber B → Ticket
Subscriber C → Slack
```

那么：

```text
Event #001
     │
     ├── Delivery #001 → CRM
     │
     ├── Delivery #002 → Ticket
     │
     └── Delivery #003 → Slack
```

这就是：

> **Fan-out**

一个 Event：

```text
1 → N
```

---

# 09. Delivery 数据模型

这是系统的核心表。

```sql
CREATE TABLE webhook_deliveries (
    id UUID PRIMARY KEY,

    event_id UUID NOT NULL,

    subscriber_id UUID NOT NULL,

    status VARCHAR(32) NOT NULL,

    attempt_count INT NOT NULL DEFAULT 0,

    next_retry_at TIMESTAMP,

    last_error TEXT,

    response_status INT,

    created_at TIMESTAMP NOT NULL,

    updated_at TIMESTAMP NOT NULL,

    UNIQUE(event_id, subscriber_id)
);
```

这里的：

```sql
UNIQUE(event_id, subscriber_id)
```

非常重要。

它保证：

> 同一个 Event 不会为同一个 Subscriber 创建多个 Delivery。

---

# 10. Delivery 状态机

建议：

```text
PENDING
   │
   ▼
DELIVERING
   │
   ├──────────────┐
   │              │
   ▼              ▼
SUCCESS         FAILED
                  │
                  ▼
               RETRYING
                  │
          ┌───────┴───────┐
          │               │
          ▼               ▼
      SUCCESS            DLQ
```

更加完整：

```text
PENDING
DELIVERING
SUCCESS
RETRYING
FAILED
DLQ
CANCELLED
```

---

# 11. 为什么需要 Delivery，而不是直接 Retry Event？

因为：

```text
Event
```

可能：

```text
CRM → success
Ticket → failure
Slack → success
```

如果你 Retry Event：

```text
Event Retry
```

会导致：

```text
CRM 再收到一次
Slack 再收到一次
```

这是错误的。

应该：

```text
Delivery #CRM
SUCCESS

Delivery #Ticket
RETRYING

Delivery #Slack
SUCCESS
```

只 Retry：

```text
Ticket Delivery
```

---

# 12. Webhook 请求结构

发送：

```http
POST /webhooks/agent
Content-Type: application/json

X-Event-ID: evt_001
X-Event-Type: agent.job.completed
X-Webhook-ID: delivery_001
X-Webhook-Timestamp: 1756774800
X-Webhook-Signature: ...
```

Body：

```json
{
  "id": "evt_001",
  "type": "agent.job.completed",
  "created_at": "...",
  "data": {
    "job_id": "job_123"
  }
}
```

---

# 13. Signature

推荐：

```text
HMAC-SHA256
```

不要直接：

```python
hash(secret + body)
```

使用：

```python
import hmac
import hashlib


def generate_signature(
    secret: str,
    timestamp: str,
    body: bytes,
) -> str:

    payload = (
        timestamp.encode()
        + b"."
        + body
    )

    digest = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return f"v1={digest}"
```

---

# 14. 为什么签名需要 Timestamp？

如果只有：

```text
signature = HMAC(body)
```

攻击者可能：

```text
截获请求
   ↓
保存
   ↓
过几小时
   ↓
重新发送
```

这叫：

> Replay Attack

因此：

```text
signature =
HMAC(
    timestamp + "." + body
)
```

服务端检查：

```python
abs(now - timestamp) < 300
```

例如只允许：

```text
5 分钟
```

---

# 15. 客户端如何验证？

客户收到：

```text
timestamp
signature
body
```

重新计算：

```python
expected = hmac(...)
```

然后：

```python
hmac.compare_digest(
    expected,
    received_signature
)
```

不能直接：

```python
expected == received
```

因为安全场景下应该使用：

```text
Constant-Time Comparison
```

避免 timing attack。

---

# 16. 重试策略

Webhook 最大的问题之一：

> 客户服务器可能暂时不可用。

例如：

```text
500
502
503
504
timeout
connection error
```

应该 Retry。

---

# 17. Exponential Backoff

例如：

```text
Attempt 1
↓
1s

Attempt 2
↓
2s

Attempt 3
↓
4s

Attempt 4
↓
8s

Attempt 5
↓
16s
```

公式：

```text
delay = min(
    max_delay,
    base_delay × 2^(attempt-1)
)
```

实际最好加入：

> **Jitter**

例如：

```text
delay = exponential_backoff + random_jitter
```

避免：

```text
10000 个 Webhook
同时失败
     ↓
同时重试
     ↓
客户服务器再次被打爆
```

这叫：

> Thundering Herd

---

# 18. 什么错误应该 Retry？

不是所有错误都 Retry。

推荐：

| HTTP | 是否 Retry |
| ---- | -------- |
| 2xx  | ❌        |
| 400  | ❌        |
| 401  | ❌        |
| 403  | ❌        |
| 404  | 通常 ❌     |
| 408  | ✅        |
| 409  | 视业务      |
| 429  | ✅        |
| 500  | ✅        |
| 502  | ✅        |
| 503  | ✅        |
| 504  | ✅        |

例如：

```text
400 Bad Request
```

继续 Retry 没意义。

但：

```text
503 Service Unavailable
```

可能只是客户服务器暂时故障。

---

# 19. Retry Policy

可以定义：

```python
@dataclass
class RetryPolicy:

    max_attempts: int = 5

    base_delay: float = 1

    max_delay: float = 300

    retry_status_codes: set[int] = field(
        default_factory=lambda: {
            408,
            429,
            500,
            502,
            503,
            504,
        }
    )
```

---

# 20. DLQ

如果：

```text
Attempt 1 ❌
Attempt 2 ❌
Attempt 3 ❌
Attempt 4 ❌
Attempt 5 ❌
```

不能：

```text
一直 Retry
```

否则：

```text
一个坏掉的 Webhook
 ↓
无限占用 Worker
```

所以：

```text
FAILED
 ↓
max_attempts
 ↓
DLQ
```

---

# 21. DLQ 保存什么？

```json
{
  "delivery_id": "delivery_001",

  "event_id": "evt_001",

  "subscriber_id": "sub_001",

  "attempt_count": 5,

  "last_error": "HTTP 503",

  "failed_at": "..."
}
```

管理员可以：

```text
查看
重新发送
取消
修改 Subscriber
```

然后：

```text
DLQ
 ↓
Replay
 ↓
PENDING
```

---

# 22. 幂等是这个项目最重要的知识点之一

Webhook 系统一般追求：

> **At-least-once delivery**

而不是：

> Exactly-once delivery

因为网络世界里：

```text
服务器收到请求
 ↓
处理成功
 ↓
返回 200 之前连接断了
```

发送方看到：

```text
timeout
```

于是：

```text
Retry
```

客户实际上会收到：

```text
第一次
第二次
```

所以：

> **Sender 保证至少一次，Receiver 必须幂等。**

---

# 23. 如何实现幂等？

请求携带：

```http
X-Event-ID: evt_001
```

客户：

```text
processed_events
```

数据库：

```sql
CREATE TABLE processed_events (
    event_id VARCHAR(128) PRIMARY KEY,
    processed_at TIMESTAMP NOT NULL
);
```

收到：

```text
evt_001
```

执行：

```text
INSERT event_id
```

如果：

```text
Primary Key Conflict
```

说明已经处理。

直接：

```text
return 200
```

---

# 24. 更推荐使用 Delivery ID

可以：

```http
X-Webhook-ID: delivery_001
```

因为：

```text
Event
```

和：

```text
Delivery
```

是不同概念。

例如：

```text
Event #001
   │
   ├── Delivery CRM #001
   ├── Delivery Ticket #002
   └── Delivery Slack #003
```

对于具体客户来说：

```text
delivery_id
```

更加准确。

---

# 25. Event Store + Queue

一个经典问题：

> Event 已经写数据库，但是 Queue publish 失败怎么办？

例如：

```text
DB INSERT
成功
 ↓
Redis publish
失败
```

结果：

```text
Event 存在
Queue 没有
```

这个 Event 就丢了。

---

# 26. Outbox Pattern

这个 Demo 非常值得实现：

```text
Application
    │
    ▼
PostgreSQL Transaction
    │
    ├── events
    │
    └── outbox_events
```

两个一起提交：

```text
BEGIN

INSERT events

INSERT outbox_events

COMMIT
```

然后：

```text
Outbox Worker
     │
     ▼
Redis Queue
```

所以：

```text
DB成功
+
Outbox成功
```

一定是原子的。

---

# 27. Outbox 表

```sql
CREATE TABLE outbox_events (
    id UUID PRIMARY KEY,

    event_id UUID NOT NULL,

    status VARCHAR(32) NOT NULL,

    created_at TIMESTAMP NOT NULL,

    published_at TIMESTAMP
);
```

Worker：

```text
SELECT
    WHERE status = 'PENDING'
```

然后：

```text
publish Redis
 ↓
status = PUBLISHED
```

---

# 28. 完整事件链路

最终：

```text
Agent
 │
 │ publish
 ▼
Event Service
 │
 ├───────────────┐
 ▼               ▼
Events          Outbox
 │               │
 │               ▼
 │          Outbox Worker
 │               │
 │               ▼
 │             Queue
 │               │
 └───────────────┘
                 │
                 ▼
             Fan-out
                 │
        ┌────────┼────────┐
        ▼        ▼        ▼
      CRM      Ticket    Slack
        │        │        │
        ▼        ▼        ▼
     SUCCESS   RETRY    SUCCESS
                  │
                  ▼
                 DLQ
```

这已经是一个非常标准的事件驱动架构。

---

# 29. 推荐技术栈

第一版：

```text
Python 3.12
FastAPI
PostgreSQL
Redis
SQLAlchemy
Pydantic
httpx
asyncio
pytest
```

Redis 用来模拟：

```text
Queue
```

PostgreSQL：

```text
Event Store
Delivery Store
Subscriber Registry
Outbox
Audit
```

---

# 30. 项目目录

建议：

```text
agent-event-fanout/
│
├── app/
│   │
│   ├── api/
│   │   ├── events.py
│   │   ├── subscribers.py
│   │   └── deliveries.py
│   │
│   ├── domain/
│   │   ├── event.py
│   │   ├── subscriber.py
│   │   └── delivery.py
│   │
│   ├── event/
│   │   ├── event_service.py
│   │   └── fanout_service.py
│   │
│   ├── webhook/
│   │   ├── sender.py
│   │   ├── signer.py
│   │   └── retry.py
│   │
│   ├── queue/
│   │   ├── producer.py
│   │   └── consumer.py
│   │
│   ├── workers/
│   │   ├── outbox_worker.py
│   │   └── webhook_worker.py
│   │
│   ├── dlq/
│   │   └── dlq_service.py
│   │
│   ├── storage/
│   │   ├── models.py
│   │   └── repository.py
│   │
│   └── security/
│       └── signature.py
│
├── tests/
│   ├── test_event.py
│   ├── test_fanout.py
│   ├── test_signature.py
│   ├── test_retry.py
│   ├── test_idempotency.py
│   └── test_dlq.py
│
├── examples/
│   ├── customer_server.py
│   └── agent.py
│
├── migrations/
│
├── docker-compose.yml
└── README.md
```

---

# 31. API 设计

## 创建 Subscriber

```http
POST /v1/subscribers
```

请求：

```json
{
  "url": "http://localhost:9001/webhook",
  "events": [
    "agent.job.completed",
    "agent.job.failed"
  ]
}
```

返回：

```json
{
  "id": "sub_001",
  "secret": "whsec_xxx"
}
```

---

# 32. 创建 Event

```http
POST /v1/events
```

请求：

```json
{
  "type": "agent.job.completed",

  "data": {
    "job_id": "job_123",
    "result": {
      "answer": "hello"
    }
  }
}
```

系统：

```text
Create Event
 ↓
Outbox
 ↓
Queue
```

立即返回：

```json
{
  "event_id": "evt_001",
  "status": "accepted"
}
```

注意：

> 不应该等待所有 Webhook 完成。

---

# 33. Fan-out Worker

逻辑：

```python
async def fanout(event):

    subscribers = await registry.match(
        event.type
    )

    for subscriber in subscribers:

        await create_delivery(
            event_id=event.id,
            subscriber_id=subscriber.id,
        )

        await queue.publish(
            delivery_id=delivery.id
        )
```

---

# 34. Webhook Worker

核心：

```python
async def deliver(delivery):

    event = await get_event(
        delivery.event_id
    )

    subscriber = await get_subscriber(
        delivery.subscriber_id
    )

    body = serialize(event)

    signature = signer.sign(
        subscriber.secret,
        body,
    )

    response = await http_client.post(
        subscriber.url,
        content=body,
        headers={
            "X-Event-ID": event.id,
            "X-Webhook-ID": delivery.id,
            "X-Webhook-Signature": signature,
        },
        timeout=10,
    )

    if is_success(response):
        await mark_success(delivery)
    else:
        await schedule_retry(delivery)
```

---

# 35. 你一定要做一个“失败客户”

例如：

```python
@app.post("/webhook")
async def webhook():

    return Response(
        status_code=503
    )
```

然后测试：

```text
Event
 ↓
Webhook
 ↓
503
 ↓
Retry
 ↓
503
 ↓
Retry
 ↓
503
 ↓
DLQ
```

观察数据库：

```text
attempt_count = 5
status = DLQ
```

这个实验非常重要。

---

# 36. 再做一个“随机失败客户”

例如：

```python
import random


@app.post("/webhook")
async def webhook():

    if random.random() < 0.5:
        return Response(
            status_code=503
        )

    return {"ok": True}
```

你会看到：

```text
Attempt 1 → 503
Attempt 2 → 503
Attempt 3 → 200
```

最终：

```text
SUCCESS
```

---

# 37. 再做一个“慢客户”

```python
@app.post("/webhook")
async def webhook():

    await asyncio.sleep(20)

    return {"ok": True}
```

客户端：

```text
timeout = 5s
```

于是：

```text
timeout
 ↓
retry
```

这个实验可以理解：

> **Webhook Timeout 本质上也是一种失败。**

---

# 38. 需要特别处理 429

客户可能：

```http
429 Too Many Requests
Retry-After: 30
```

你的系统最好：

```python
retry_after = response.headers.get(
    "Retry-After"
)
```

然后：

```text
next_retry_at = now + retry_after
```

而不是无脑：

```text
exponential backoff
```

这体现了：

> **服务端显式 backpressure。**

---

# 39. Webhook Delivery 的状态查询

```http
GET /v1/deliveries/{delivery_id}
```

返回：

```json
{
  "id": "delivery_001",

  "event_id": "evt_001",

  "subscriber_id": "sub_001",

  "status": "retrying",

  "attempt_count": 3,

  "last_error": "HTTP 503",

  "next_retry_at": "..."
}
```

这样前端或者运营系统可以看到：

```text
Webhook
 ├── Success
 ├── Retry
 └── Dead Letter
```

---

# 40. Metrics

这个项目建议至少做这些 Metrics：

```text
webhook_events_total

webhook_deliveries_total

webhook_delivery_success_total

webhook_delivery_failed_total

webhook_delivery_retry_total

webhook_dlq_total

webhook_delivery_latency

webhook_delivery_attempts
```

尤其是：

```text
Success Rate
Retry Rate
DLQ Rate
P95 Delivery Latency
```

---

# 41. 一个很重要的指标：Delivery Lag

定义：

```text
delivery_lag =
delivery_success_time
-
event_created_time
```

例如：

```text
Event Created
10:00:00

Webhook Success
10:00:03

Delivery Lag
3s
```

对于异步 Agent：

> **Event 完成了不代表客户已经收到。**

因此：

```text
Agent Completion Latency
```

和：

```text
Webhook Delivery Latency
```

应该分开监控。

---

# 42. 和你的 Async Agent Job Queue 连接

这个项目和你前一个：

```text
06 Async Agent Job Queue
```

天然连接。

之前：

```text
POST /jobs
 ↓
Job Queue
 ↓
Worker
 ↓
Job Completed
```

现在：

```text
Job Completed
      │
      ▼
Event
      │
      ▼
Webhook Fan-out
      │
 ┌────┼────┐
 ▼    ▼    ▼
CRM  Ticket Slack
```

所以整个链路变成：

```text
User
 │
 ▼
Job API
 │
 ▼
Queue
 │
 ▼
Agent Worker
 │
 ▼
Checkpoint
 │
 ▼
Job Completed
 │
 ▼
Event Bus
 │
 ▼
Webhook Fan-out
 │
 ├── CRM
 ├── Ticket
 └── Slack
```

这就是一个完整的：

> **异步 Agent → 事件驱动 → 企业工作流集成**

---

# 43. 和 Prompt Versioning 连接

你前面第 09 个项目：

```text
Prompt & Config Versioning
```

如果 Agent 使用：

```text
config v13
prompt v20
```

完成任务：

```text
agent.job.completed
```

Event 中可以记录：

```json
{
  "type": "agent.job.completed",

  "metadata": {
    "agent": "test_case_agent",
    "config_version": 13,
    "prompt_version": 20,
    "model": "qwen3.5-27b"
  }
}
```

以后客户说：

> 为什么这个测试用例和昨天生成的不一样？

你可以沿着：

```text
Event
 ↓
Job
 ↓
Config Version
 ↓
Prompt Version
 ↓
Model
```

完整追踪。

---

# 44. 和 Token Metering 连接

Event：

```json
{
  "type": "agent.job.completed",

  "usage": {
    "input_tokens": 12000,
    "output_tokens": 3000,
    "cost": 0.18
  }
}
```

于是可以把：

```text
Agent Job
 ↓
Token Usage
 ↓
Billing
 ↓
Webhook
```

全部串起来。

例如客户自己的财务系统可以订阅：

```text
billing.usage.created
```

---

# 45. 最后形成你的 Agent Infra 学习路线

你现在这几个项目实际上已经可以组合成：

```text
01 LLM Gateway
        │
        ▼
02 Token Metering
        │
        ▼
03 Streaming
        │
        ▼
04 Semantic Cache
        │
        ▼
06 Async Job Queue
        │
        ▼
07 Tool Sandbox
        │
        ▼
09 Prompt Config Registry
        │
        ▼
10 Webhook Event Fan-out
```

再往后：

```text
11 Observability
12 Agent Memory
13 Evaluation
14 Multi-Agent Runtime
15 Workflow Engine
```

最后变成：

```text
                         Agent Platform
                              │
       ┌──────────────────────┼──────────────────────┐
       ▼                      ▼                      ▼
 Config Plane            Runtime Plane          Integration
       │                      │                      │
 Prompt Registry          Agent Runtime          Event Bus
 Model Config             Tool Execution         Webhook
 Versioning               Job Queue              Kafka
 A/B Router               Sandbox                Workflow
       │                      │                      │
       └──────────────────────┼──────────────────────┘
                              ▼
                        LLM Gateway
                              │
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
        Rate Limit        Semantic Cache    Token Meter
             │                │                │
             └────────────────┼────────────────┘
                              ▼
                             LLM
```

---

# 46. 这个 Demo 最值得你手写的 8 个实验

不要只完成 CRUD。我建议直接按照这 8 个实验验收：

```text
实验 1
一个 Event → 三个 Subscriber
验证 Fan-out

实验 2
Subscriber 返回 500
验证 Exponential Backoff

实验 3
Subscriber 返回 429
验证 Retry-After

实验 4
Subscriber 永久失败
验证 DLQ

实验 5
同一个 Delivery 重复消费
验证 Idempotency

实验 6
HTTP 请求成功但响应丢失
模拟 At-least-once

实验 7
DB 成功但 Queue publish 失败
验证 Outbox Pattern

实验 8
伪造 Webhook Body / Timestamp
验证 HMAC Signature + Replay Protection
```

其中 **实验 5、6、7、8** 是最有面试价值的。

尤其是第 6 个，你应该能够明确回答：

> **Webhook 为什么通常只能做到 At-least-once，而不是 Exactly-once？**

因为：

```text
Server 已经执行
        │
        ▼
准备返回 200
        │
        X 网络断开
        │
        ▼
Sender 不知道是否成功
        │
        ▼
Retry
```

所以系统无法仅靠网络协议判断：

```text
“对方到底有没有执行成功”
```

最终只能：

```text
Sender:
At-least-once

Receiver:
Idempotency
```

这也是这个 Demo 和普通“HTTP 回调 Demo”最大的区别。

**如果你的目标是从 0 学 Agent Infra，我建议这个项目暂时不要上 Kafka、RabbitMQ、Kubernetes。第一版用 `FastAPI + PostgreSQL + Redis + httpx` 把 Event Store、Outbox、Fan-out、Delivery State Machine、Retry、DLQ、HMAC、Idempotency 全部手写出来；等这一版彻底理解后，再把 Redis Queue 替换成 Kafka，去研究 partition、consumer group、offset、rebalancing 和 delivery semantics。**
