# agent-event-fanout

> Webhook 与事件分发系统 Demo —— **Event Store / Outbox / Fan-out / Delivery 状态机 / Retry / DLQ / HMAC 签名 / 幂等**

对应设计说明书：`第八章：Webhook 与事件分发系统设计说明书.md`

一个 Agent 完成任务后，要把结果可靠地通知 CRM、工单系统、Slack 等多个客户系统。
本 Demo 用 **FastAPI + SQLite/PostgreSQL + Redis + httpx** 从零手写这套事件分发基础设施，
重点演示「**At-least-once 投递 + Receiver 幂等**」与「**Outbox Pattern**」这两个生产级设计。

---

## 01. 架构

```
                    Agent / Job
                         │ publish event
                         ▼
                ┌─────────────────┐
                │  Event Ingress  │  POST /v1/events
                └────────┬────────┘
                         ▼
              Event + Outbox（同一事务，§26 Outbox Pattern）
                         │
                         ▼
                ┌─────────────────┐
                │   Event Store   │  events / outbox_events
                │   SQLite/PG     │
                └────────┬────────┘
                         │ Outbox Worker
                         ▼
                ┌─────────────────┐
                │   Event Queue   │  Redis / 内存
                └────────┬────────┘
                         │ Fan-out Consumer
                         ▼
                 ┌───────┼────────┐
                 ▼       ▼        ▼
              Worker   Worker   Worker     为每个 Subscriber 建 Delivery（PENDING）
                 │       │        │
                 ▼       ▼        ▼
              CRM     Ticket    Slack       HTTP POST + HMAC 签名
                 │       │        │
                 ▼       ▼        ▼
            SUCCESS   RETRY    SUCCESS     指数退避 + 抖动 / Retry-After
                          │
                          ▼
                         DLQ               max_attempts 后进死信队列，可 Replay
```

**第一原则（§03）：Event 和 Delivery 必须分离。**

- `Event` = 发生了什么（不可变，1 条）。
- `Delivery` = 这个事件发送给谁、发到什么状态（1 → N）。

如果三个客户里 CRM 成功、Ticket 超时、Slack 失败，你只会重试 **Ticket 的那条 Delivery**，
而不会让 CRM / Slack 再收到一次。

---

## 02. 核心概念

| 概念 | 说明 | 设计说明书 |
| --- | --- | --- |
| Event | 不可变事件，`agent.job.completed` 等 | §04~§05 |
| Subscriber | 谁订阅 + 订阅什么 + 发到哪 + 怎么验证 | §06~§07 |
| Fan-out | 1 个 Event → N 个 Delivery | §08, §33 |
| Delivery 状态机 | PENDING→DELIVERING→SUCCESS / FAILED→RETRYING→DLQ | §09~§11 |
| HMAC 签名 | `v1=HMAC(secret, timestamp.body)`，防重放 + 防伪造 | §13~§15 |
| 指数退避 + 抖动 | 避免 Thundering Herd | §17 |
| Retry-After | 尊重客户 429 的显式 backpressure | §38 |
| DLQ | 超过 max_attempts 不再重试，可查看/Replay/取消 | §20~§21 |
| 幂等 | `UNIQUE(event_id, subscriber_id)` + 原子领取 | §09, §23 |
| Outbox | 事件与队列发布解耦，DB 成功 ⇔ 最终一定入队 | §25~§27 |
| Metrics | Prometheus 文本格式，含 P95 latency | §40~§41 |

---

## 03. 快速开始（零外部依赖）

默认后端：SQLite（持久化）+ 内存队列，**不需要 Docker / Redis / PostgreSQL**。

```bash
pip install -e .[sqlite,test]

# 1) 启动 API（内置 Outbox / Fan-out / Webhook 三个后台 Worker）
uvicorn app.main:app --port 8000

# 2) 另开终端：注册订阅者
curl -X POST http://localhost:8000/v1/subscribers \
  -H "Content-Type: application/json" \
  -d '{"url":"http://localhost:9001/webhook","events":["agent.job.completed"]}'
# 返回 {"id":"sub_xxx","secret":"whsec_xxx"}  <- 记下 secret

# 3) 模拟 Agent 发布事件
python examples/agent.py

# 4) 查询投递状态
curl http://localhost:8000/v1/deliveries
# 成功: status=SUCCESS；失败重试: status=RETRYING；死信: status=DLQ
```

### 一键跑通 8 个实验（§46）

```bash
python experiments/run_experiments.py
```

输出类似：

```
[实验1] Fan-out: 1 事件 -> 3 条 Delivery，全部 SUCCESS
[实验2] 指数退避: 重试间隔 = [1.0, 2.0] -> 最终 SUCCESS
[实验3] Retry-After: 429 时下次重试 = now+30s
[实验4] DLQ: 5 次失败后 status=DLQ, attempt_count=5
[实验5] 幂等: 重复消费同一 Delivery，HTTP 实际只发生 1 次
[实验6] At-least-once: 响应丢失后重试，客户收到 2 次；幂等键相同=True
[实验7] Outbox: Queue 故障时发布数=0（PENDING=1，事件不丢），恢复后补发=1
[实验8] 签名: 伪造 body -> HMAC 不匹配 -> 拒绝 = True
```

### 真实端到端冒烟（可选）

起真实 API + 真实客户服务器，验证 **HMAC 签名校验 + 幂等** 全链路：

```bash
python experiments/e2e_smoke.py
# 订阅者: sub_xxx secret: whsec_xxx...
# 投递: SUCCESS attempts: 1
# 客户已处理数: {'processed': 1}
```

> 提示：如果本机设置了**系统代理**（Windows 注册表 / HTTP_PROXY），
> `httpx` 默认会走代理，把发往 localhost 和客户 Webhook 的请求拦成 503。
> 本项目的 Webhook 发送器已用 `trust_env=False` 直连（`app/webhook/sender.py`），
> 这是 Webhook 投递服务的正确默认 —— 出站投递不应隐式经过系统代理。

### 跑测试

```bash
pytest -q     # 63 tests，覆盖全部 8 个实验 + API
```

---

## 04. 真实客户实验（§35~§37）

用真实的失败客户观察重试与 DLQ：

```bash
# 终端 A：失败客户（永远 503）
uvicorn examples.failing_customer:app --port 9002

# 终端 B：注册订阅者指向它
curl -X POST http://localhost:8000/v1/subscribers \
  -H "Content-Type: application/json" \
  -d '{"url":"http://localhost:9002/webhook","events":["agent.job.completed"]}'

# 终端 C：发布事件，然后反复查询
curl -X POST http://localhost:8000/v1/events \
  -H "Content-Type: application/json" \
  -d '{"type":"agent.job.completed","data":{"job_id":"job_123"}}'
curl -s http://localhost:8000/v1/deliveries | python -m json.tool
# 观察 attempt_count 递增 -> 5 次后 status = DLQ
```

其他客户：`random_failing_customer.py`（50% 503）、`slow_customer.py`（睡 20s，观察超时重试）、
`ratelimit_customer.py`（429 + Retry-After）。

`examples/customer_server.py` 是**正常客户**，展示了 §15 签名校验 + §23 幂等去重的正确写法。

---

## 05. 切换到 PostgreSQL + Redis

本机装不了 Docker 时，代码仍是 SQLite 后端（同一套 SQL，见 `migrations/001_init_postgres.sql`）。
有 Docker 时：

```bash
docker compose up -d postgres redis

pip install -e .[postgres,redis]

DATABASE_URL=postgresql://fanout:fanout@localhost:5432/agent_event_fanout \
QUEUE_BACKEND=redis REDIS_URL=redis://localhost:6379/0 \
uvicorn app.main:app --port 8000
```

---

## 06. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/subscribers` | 创建订阅者，返回 `id` + `secret`（§31） |
| GET | `/v1/subscribers` | 列出订阅者 |
| PATCH | `/v1/subscribers/{id}` | 启停 / 改订阅事件 |
| POST | `/v1/events` | 创建事件，立即返回 `accepted`（§32） |
| GET | `/v1/events/{id}` | 查看事件 |
| GET | `/v1/deliveries?status=DLQ` | 查询投递（可按状态过滤） |
| GET | `/v1/deliveries/{id}` | 查看单条投递状态（§39） |
| POST | `/v1/deliveries/{id}/replay` | DLQ → PENDING 重新投递（§21） |
| POST | `/v1/deliveries/{id}/cancel` | 取消投递 |
| GET | `/metrics` | Prometheus 指标（§40） |
| GET | `/healthz` | 健康检查 |

---

## 07. 目录结构

```
agent-event-fanout/
├── app/
│   ├── api/            # events / subscribers / deliveries + schemas
│   ├── domain/         # Event / Subscriber / Delivery(状态机) / Outbox
│   ├── event/          # event_service（Outbox 事务）+ fanout_service（扇出）
│   ├── webhook/        # signer / sender / retry（退避+Retry-After）/ serializer
│   ├── queue/          # EventQueue 抽象：memory / redis
│   ├── workers/        # outbox_worker / webhook_worker
│   ├── dlq/            # dlq_service（查看 / Replay / 取消）
│   ├── storage/        # models（DDL）+ repository（接口 + SQLite）
│   ├── security/       # signature（HMAC + Replay Protection）
│   ├── metrics.py      # 计数器 + Prometheus 文本输出
│   ├── config.py       # 环境变量配置
│   ├── factory.py      # 依赖装配 Runtime
│   └── main.py         # FastAPI 入口
├── tests/              # 63 个测试，覆盖 8 个实验
├── examples/           # 客户服务器 / 失败客户 / Agent 发布器 / 签名校验
├── experiments/        # 8 个实验一键验收脚本
├── migrations/         # PostgreSQL 初始 DDL
├── docker-compose.yml  # Postgres + Redis
└── README.md
```

---

## 08. 为什么 Webhook 只能做到 At-least-once（§46 实验 6）

```
服务端已执行
   │
   ▼
准备返回 200
   │
   X  网络断开
   │
   ▼
发送方不知道是否成功
   │
   ▼
Retry
```

Sender 无法仅靠网络协议判断「对方到底有没有执行成功」，所以：

> **Sender：At-least-once；Receiver：Idempotency（用 `X-Webhook-ID` 去重）。**

这就是本 Demo 和普通「HTTP 回调 Demo」最大的区别。

---

## 09. 与上一个项目（异步 Agent Job Queue）的连接（§42）

```
POST /jobs → Job Queue → Agent Worker → Job Completed
                                              │
                                              ▼
                                          Event（本系统）
                                              │
                                              ▼
                                        Webhook Fan-out
                                          ├── CRM
                                          ├── Ticket
                                          └── Slack
```

Event 里可带上 `config_version / prompt_version / model`（§43）与 token 用量（§44），
客户或运营系统可以沿 Event → Job → Config → Prompt → Model 完整追溯「为什么结果不一样」。
