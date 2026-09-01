# StreamInfra

**LLM 流式响应基础设施（LLM Streaming Response Infrastructure）Demo**

根据《第三章：LLM 流式响应基础设施设计说明书.md》实现的简化版 Streaming Infrastructure。

> **流式基础设施的核心不是"把字符串 yield 出去"，而是建立一套可靠的事件流传输机制。**

```text
Client
  │
  ▼
Stream Gateway
  │
  ├── SSE  ──┐
  │          ├── Stream Manager ──┬── Event Buffer (Replay)
  └── WS  ───┘                    ├── Sequence (seq)
                                  ├── Backpressure (有界队列)
                                  ├── Disconnect → 取消上游
                                  └── Resume / Replay (Last-Event-ID)
                          │
                          ▼
                     LLM Provider (Mock)
                          │
                          ▼
                     Metrics (TTFT/TPOT/Total Latency)
```

---

## 1. 特性

- **SSE**：`GET /v1/chat/stream`，`id`/`event`/`data` 三字段帧格式，心跳 comment，`data: [DONE]` 结束符。
- **WebSocket**：`ws://localhost:8000/v1/ws`，start / cancel / ping 控制消息。
- **Backpressure**：有界 `asyncio.Queue` 阻塞 Producer；支持 4 种策略（BLOCK / DROP_OLDEST / DROP_NEWEST / DISCONNECT）；`max_queue_wait` 超时自动取消整条流。
- **Disconnect**：检测客户端断开 → 取消下游 → 取消上游 LLM → 记录 usage。
- **Reconnect / Replay**：`Last-Event-ID` + Replay Buffer（定长 `deque(maxlen=N)`）续传；超出窗口返回 `409 resume_window_expired`。
- **部分失败**：Streaming 开始后绝不返回 HTTP 500，错误变成 `error` + `done(reason=error)` 事件。
- **统一事件模型**：`StreamEvent(stream_id, seq, type, data)`，事件类型 `start / metadata / token / tool_call / error / done / heartbeat`。
- **Metrics**：`/metrics`（Prometheus 文本格式），含 TTFT / TPOT / Throughput / Total Latency 与各类计数器。
- **Mock LLM**：可注入延迟、失败点、tool_call，便于稳定测试。
- **Redis 可选**：`buffer/redis.py` 用 Redis Stream 持久化 Replay Buffer（默认内存）。

---

## 2. 快速开始

```bash
# 安装（Python >= 3.10）
pip install -e .

# 启动
uvicorn streaminfra.main:app --reload --port 8000
```

### 2.1 SSE Demo

```bash
curl -N http://localhost:8000/v1/chat/stream?prompt=%E4%BD%A0%E5%A5%BD
```

输出示例：

```
id: 1
event: metadata
data: {"model": "mock-llm-1", "input_tokens": 120, "prompt": "你好"}

id: 2
event: token
data: {"delta": "你"}

id: 3
event: token
data: {"delta": "好"}

...

id: N
event: done
data: {"reason": "completed"}

data: [DONE]
```

### 2.2 断线重连 Demo（§49 验收场景）

```python
import httpx

base = "http://localhost:8000"

# 第一次连接：收到前 3 个 token 后断开
with httpx.Client(base_url=base, timeout=20) as c:
    with c.stream("GET", "/v1/chat/stream", params={"prompt": "hi"}) as r:
        stream_id = r.headers["x-stream-id"]
        for line in r.iter_lines():
            if line.startswith("id: "):
                last_seq = int(line[4:])
            if line.startswith("event: token"):
                if int(...)  ...  # 记录 seq，读到 3 个就 break

    # 重连：浏览器 SSE 会自动带上 Last-Event-ID 头
    with c.stream(
        "GET", "/v1/chat/stream",
        params={"stream_id": stream_id},
        headers={"Last-Event-ID": str(last_seq)},
    ) as r2:
        for line in r2.iter_lines():
            print(line)  # 从 seq = last_seq + 1 继续，直到 DONE
```

### 2.3 WebSocket Demo

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://localhost:8000/v1/ws") as ws:
        await ws.send(json.dumps({"type": "start", "prompt": "hi"}))
        print(await ws.recv())                      # {"type": "started", "stream_id": ...}
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == "done":
                break
            print(msg)                              # {"type": "token", "seq": 1, "delta": "你"}
    # 断线重连续传：带上 stream_id + last_seq

asyncio.run(main())
```

---

## 3. API 参考

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/chat/stream` | SSE 流。参数 `stream_id`（续传）/ `prompt` / `last_seq`；请求头 `Last-Event-ID` 优先 |
| WS | `/v1/ws` | WebSocket 流。客户端发 `start` / `cancel` / `ping` |
| GET | `/v1/streams/{stream_id}/result` | 流的最终/部分结果（status + content + usage + error） |
| GET | `/metrics` | Prometheus 指标 |
| GET | `/health` | 健康检查 |

### 3.1 SSE 状态码

- `200` 流式响应开始（含 `X-Stream-Id` 头）
- `404` `stream_not_found`：指定了不存在的 stream_id
- `409` `resume_window_expired`：`last_seq` 超出 Replay Window（§25）
  - `reason: "behind"`：`last_seq` 已被窗口淘汰（无法重放）
  - `reason: "ahead"`：`last_seq` 超过服务端已产出的最大 seq（非法游标，防止连接挂起）
- `409` `concurrent_consumer`：该流已有活跃订阅者（单消费者模型）

### 3.2 WebSocket 客户端消息

```json
{"type": "start", "stream_id": "...", "prompt": "...", "last_seq": 3}
{"type": "cancel"}
{"type": "ping"}
```

服务端消息：`{"type": "started|resumed|token|metadata|tool_call|error|done|heartbeat|pong", "seq": N, ...}`

---

## 4. 核心设计

### 4.1 为什么必须有 seq（§6）

断线重连需要知道"客户端已经收到哪个事件"。`Last-Event-ID: 3` 意味着服务端从 `seq > 3` 继续。

### 4.2 为什么用有界队列（§11 / §12）

LLM 1000 token/s、Client 10 token/s 时，无限队列每秒堆积 990 条 → 内存上涨 → OOM。
有界队列 + 阻塞 Producer 是 LLM Token 场景的默认背压策略（§13 Strategy A）。
`max_queue_wait` 超时则取消整条流。

### 4.3 断线必须取消上游（§19 / §20 / §21）

```text
Client 断开 → Gateway 检测 → cancel 下游 → cancel 上游 LLM → finalize usage
```

否则 Provider 继续生成，Token 没人消费，平台仍然付费。

### 4.4 为什么不能返回 HTTP 500（§43 / §44）

HTTP Status 在 Streaming 开始后已无法修改（Header 已发出 `200 OK`）。
错误必须变成 Stream Event：`error` → `done(reason="error")`，客户端仍能拿到已生成的部分结果（§26 / §27）。

### 4.5 Replay Window（§25）

Replay Buffer 只保留最近 N 条（默认 1000）。`last_seq` 校验同时检查上下界：
- 落后：`last_seq` 早于窗口最旧 seq，无法假装恢复 → `409 resume_window_expired (behind)`。
- 超前：`last_seq` 超过服务端已产出的最大 seq（非法/恶意游标），若不拦截会因去重
  跳过所有事件导致连接永久挂起 → `409 resume_window_expired (ahead)`。

### 4.6 单消费者模型（§10）

同一条流同一时刻只允许一个活跃订阅者（Producer → Queue → Consumer）。第二个订阅者
返回 `409 concurrent_consumer`，避免多消费者从同一把有界队列瓜分事件、`done` 只达一人。
断线重连前旧订阅者会先释放消费者槽位，因此重连不会被误判为并发。

### 4.7 终止事件与背压

`error` / `done` 等终止事件优先参与背压（等消费者腾出空间），不会挤占/丢弃尚未发送的
业务 Token（§26 "已经发出的 Token 不能丢"）。仅当消费者长时间不再消费（超过
`max_queue_wait`）时才丢弃最旧事件以保证终止必然到达。

---

## 5. Metrics（§30 / §31）

| 指标 | 含义 |
| --- | --- |
| `stream_requests_total` | 创建流总数 |
| `stream_completed_total` | 正常完成数 |
| `stream_failed_total` | 失败数 |
| `stream_cancelled_total` | 取消数 |
| `stream_disconnect_total` | 客户端断线数 |
| `stream_reconnect_total` | 重连数 |
| `stream_replay_total` | 重放数 |
| `stream_backpressure_total` | 背压事件数 |
| `stream_ttft_seconds` | TTFT：Request → First Token |
| `stream_total_latency_seconds` | Total Latency：Request → DONE |
| `stream_tpot_seconds` | Time Per Output Token |
| `stream_output_tokens` | 输出 token 数 |

> 为什么关注 TTFT 而不是平均响应时间（§29）？
> 用户感知的速度是"什么时候开始看到结果"：TTFT=300ms/Total=10s 的模型，
> 通常比 TTFT=5s/Total=7s 的模型让用户感觉更快。

---

## 6. 目录结构（§39）

```text
stream-infra/
├── README.md
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile
├── streaminfra/
│   ├── config.py              # StreamConfig
│   ├── main.py                # create_app() 装配
│   ├── api/
│   │   ├── sse.py             # GET /v1/chat/stream
│   │   └── websocket.py       # ws /v1/ws
│   ├── core/
│   │   ├── event.py           # StreamEvent / EventType / StreamError
│   │   ├── state.py           # StreamStatus 状态机
│   │   ├── stream.py          # Stream：Producer 编排 + 发布/取消/部分失败
│   │   └── manager.py         # StreamManager：create/subscribe/replay/cancel
│   ├── transport/
│   │   ├── sse.py             # SSE 编解码 + 心跳 comment
│   │   └── websocket.py       # WS 扁平 JSON 编解码
│   ├── buffer/
│   │   ├── base.py            # ReplayBuffer 抽象
│   │   ├── memory.py          # 内存 deque(maxlen=N)
│   │   └── redis.py           # Redis Stream（可选）
│   ├── backpressure/
│   │   └── controller.py      # 有界队列 + 4 种策略 + max_queue_wait
│   ├── provider/
│   │   └── mock_llm.py        # Mock LLM（可注入失败/tool_call/延迟）
│   └── metrics/
│       └── latency.py         # MetricsRegistry + Prometheus 渲染
└── tests/
    ├── unit/                  # test_event / test_buffer / test_backpressure
    ├── integration/           # test_sse / test_websocket / test_reconnect
    └── chaos/                 # test_disconnect / test_upstream_failure / test_slow_client
```

---

## 7. 测试

```bash
pip install -e ".[test]"
python -m pytest -q
```

覆盖：

- **unit**：事件模型、Replay Buffer 窗口淘汰、背压四策略。
- **integration**：SSE 全流程 / 404 / 409、WebSocket 全流程与取消重连、`Last-Event-ID` 重放、管理器级断线续传。
- **chaos**：断线取消上游（管理器 + 真实 uvicorn）、上游部分失败（不返回 500）、慢消费者背压超时。

> 说明：ASGITransport 不会在客户端中途关闭连接时向应用发送 `http.disconnect`，
> 因此断线/慢客户端两类场景使用真实 uvicorn 服务器验证（`conftest.live_server`）。
> 慢客户端在 HTTP 层难以稳定复现（响应体较小时会被 TCP 发送缓冲吸收），
> 故在管理器层面直接模拟"Producer 快、Consumer 慢"。

---

## 8. Docker / Redis

```bash
# 仅起 Redis（便于体验 Redis Stream 后端）
docker compose up redis

# 全部（app + redis）
docker compose up --build
```

切换 Redis 后端：

```bash
STREAM_BUFFER_BACKEND=redis STREAM_REDIS_URL=redis://localhost:6379/0 uvicorn streaminfra.main:app
```

---

## 9. 与其它项目的关系（§47）

本 Demo 是"LLM Gateway 基础设施"三件套之一：

```text
Client → OpenGateway → RateLimiter → Model Router → StreamInfra → LLM → TokenMeter → Pricing → Billing
```

- StreamInfra 负责流式传输（SSE/WS）、背压、断线恢复、TTFT/TPOT 指标。
- TokenMeter 负责 `input_tokens / output_tokens` 计量（`/v1/streams/{id}/result` 的 `usage` 即为此预留）。

---

## 10. 设计说明书要点速查

| 问题（§48） | 答案 / 实现位置 |
| --- | --- |
| SSE 和 WebSocket 有什么区别？为什么 LLM Token 常用 SSE？ | SSE 单向、HTTP、自动重连；LLM 典型模式就是 Server→Token 单向流 → `transport/sse.py` |
| LLM 100 token/s、Client 10 token/s 怎么办？ | 有界队列 + 阻塞 Producer → `backpressure/controller.py` |
| 为什么不能无限缓存 Event？ | 内存 OOM → 定长 Replay Buffer → `buffer/memory.py` |
| 用户关闭浏览器后 Provider 还在生成怎么办？ | 取消链路传播 → `stream.cancel()` / `manager.disconnect()` |
| Gateway 如何把取消传播到上游？ | `asyncio.CancelledError` 沿调用链传播 → `stream.py:_run_producer` |
| Client 收到 1~100 后断线，重连从哪开始？ | `Last-Event-ID` + Replay Buffer → `manager.subscribe(last_seq)` |
| last_seq 超出 Replay Buffer 怎么办？ | `409 resume_window_expired` → `buffer/base.py:ResumeWindowExpired` |
| 已返回 100 个 Token 后 Provider 异常，HTTP Status？ | 不能改 500；发 `error` + `done(reason=error)` → `stream.py` |
| 为什么关注 TTFT？ | 用户感知的首 token 时延 → `metrics/latency.py` |
| Gateway 10 个实例，Stream State 放哪？ | 生产建议 Redis Stream → `buffer/redis.py`（本 Demo 默认内存） |
| 为什么 Redis Stream 比 Pub/Sub 更适合 Replay？ | Pub/Sub 断线丢消息；Stream 保留消息 → `buffer/redis.py` |
