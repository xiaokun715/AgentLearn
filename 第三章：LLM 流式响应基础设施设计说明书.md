# StreamInfra

## LLM 流式响应基础设施设计说明书

**Version:** v0.1
**Language:** Python 3.12
**Framework:** FastAPI + asyncio
**Status:** Demo / Learning Project

---

# 1. 项目概述

## 1.1 项目背景

传统 LLM API：

```text
Client
   │
   │ Request
   ▼
LLM
   │
   │ 等待完整结果
   ▼
Response
```

用户必须等待模型生成完成后才能看到结果。

对于长文本生成：

```text
Time
│
├─────────────── LLM Generation ────────────────┤
│                                                │
│                                                │
│                                                ▼
│                                           First Response
```

用户体验非常差。

流式响应改成：

```text
Time
│
├── TTFT ──┤
│          │
│          ▼
│        Token
│          ↓
│        Token
│          ↓
│        Token
│          ↓
│        Token
│          ↓
│        DONE
```

用户可以在模型生成过程中持续看到结果。

因此：

> **流式基础设施的核心不是“把字符串 yield 出去”，而是建立一套可靠的事件流传输机制。**

---

# 2. 项目目标

实现一个简化版 LLM Streaming Infrastructure：

```text
Client
  │
  ▼
Stream Gateway
  │
  ├── SSE
  │
  └── WebSocket
  │
  ▼
Stream Manager
  │
  ├── Event Buffer
  ├── Sequence
  ├── Backpressure
  ├── Disconnect
  └── Resume
  │
  ▼
LLM Provider
```

---

# 3. 核心功能

必须实现：

### 3.1 SSE

```http
GET /v1/chat/stream
```

返回：

```text
data: {"seq":1,"delta":"你"}

data: {"seq":2,"delta":"好"}

data: {"seq":3,"delta":"，"}

data: {"seq":4,"delta":"世界"}

data: [DONE]
```

---

### 3.2 WebSocket

```text
ws://localhost:8000/v1/ws
```

服务器：

```json
{
    "type": "token",
    "seq": 1,
    "delta": "你"
}
```

---

### 3.3 Backpressure

处理：

```text
Producer
   │
   │ 快
   ▼
Queue
   │
   │ 慢
   ▼
Consumer
```

当 Consumer 太慢时：

```text
Queue Full
   ↓
Producer Block
```

不能无限堆积内存。

---

### 3.4 Disconnect

Client：

```text
connected
   ↓
token1
token2
token3
   ↓
disconnect
```

Gateway 必须：

```text
detect disconnect
      ↓
cancel upstream
      ↓
finalize stream
      ↓
record usage
```

---

### 3.5 Reconnect

Client：

```text
connect
 ↓
receive seq 1
receive seq 2
receive seq 3
 ↓
network failure
```

重新连接：

```text
Last-Event-ID: 3
```

Gateway：

```text
seq > 3
```

继续发送：

```text
seq 4
seq 5
seq 6
...
```

---

# 4. 为什么选择 SSE

LLM Token Streaming 天然适合 SSE。

SSE：

```text
Server
   │
   │ one-way stream
   ▼
Client
```

LLM 的典型通信模式就是：

```text
Client → Request

Server → Token
Server → Token
Server → Token
Server → Token
Server → DONE
```

所以 SSE 足够简单。

---

# 5. SSE 数据格式

建议定义统一 Event Schema：

```json
{
    "id": "stream_001",
    "seq": 1,
    "type": "token",
    "data": {
        "delta": "你"
    },
    "timestamp": 1756710000
}
```

事件类型：

```text
token
metadata
tool_call
error
done
heartbeat
```

例如：

```text
token
```

表示：

```json
{
    "type": "token",
    "seq": 10,
    "data": {
        "delta": "你好"
    }
}
```

---

# 6. 为什么必须有 seq

不能只有：

```json
{
    "delta": "你好"
}
```

必须有：

```json
{
    "seq": 100,
    "delta": "你好"
}
```

因为断线重连需要知道：

> **Client 已经收到哪个事件。**

例如：

```text
1
2
3
4
5
6
```

Client 收到：

```text
1
2
3
```

然后断线。

重新连接：

```text
Last-Event-ID: 3
```

Server 从：

```text
4
```

开始继续发送。

---

# 7. Stream Event

Python：

```python
from dataclasses import dataclass
from typing import Any


@dataclass
class StreamEvent:
    stream_id: str
    seq: int
    type: str
    data: Any
```

例如：

```python
StreamEvent(
    stream_id="stream_001",
    seq=10,
    type="token",
    data={
        "delta": "hello"
    }
)
```

---

# 8. Stream State

每个 Stream 需要维护状态：

```python
from enum import Enum


class StreamStatus(str, Enum):

    CREATED = "created"

    RUNNING = "running"

    COMPLETED = "completed"

    FAILED = "failed"

    CANCELLED = "cancelled"
```

完整状态：

```text
CREATED
   │
   ▼
RUNNING
   │
   ├─────────────┐
   ▼             ▼
COMPLETED      FAILED
   │
   ▼
CLOSED
```

异常断开：

```text
RUNNING
   │
   ▼
CANCELLED
```

---

# 9. StreamManager

核心组件：

```python
class StreamManager:

    async def create_stream():
        ...

    async def publish(
        self,
        event: StreamEvent
    ):
        ...

    async def subscribe(
        self,
        stream_id: str
    ):
        ...

    async def replay(
        self,
        stream_id: str,
        last_seq: int
    ):
        ...

    async def close(
        self,
        stream_id: str
    ):
        ...
```

---

# 10. Producer / Consumer 模型

Streaming 本质上是一个：

> Producer → Consumer

模型。

```text
                    Stream
                      │
              ┌───────┴───────┐
              │               │
           Producer         Consumer
              │               │
              ▼               ▼
          LLM Provider      Client
```

Producer：

```text
LLM Token
```

Consumer：

```text
Browser / SDK
```

Gateway 位于中间：

```text
LLM
 ↓
Producer
 ↓
Queue
 ↓
Consumer
 ↓
Client
```

---

# 11. Async Queue

最简单的背压机制：

```python
queue = asyncio.Queue(maxsize=100)
```

Producer：

```python
await queue.put(event)
```

Consumer：

```python
event = await queue.get()
```

如果：

```text
Queue size = 100
```

那么：

```text
Producer
   │
   ▼
┌─────────────────┐
│ Queue           │
│ 100 / 100       │
└─────────────────┘
        │
        ▼
     Consumer
```

Producer 执行：

```python
await queue.put(event)
```

会等待。

这就是：

> **Backpressure**

---

# 12. 为什么不能无限 Queue

错误：

```python
queue = asyncio.Queue()
```

如果：

```text
LLM = 1000 tokens/s

Client = 10 tokens/s
```

那么：

```text
每秒积累：
990 tokens
```

最终：

```text
Memory
   ↓
不断上涨
   ↓
OOM
```

因此：

```text
bounded queue
```

非常重要。

---

# 13. Backpressure 策略

当 Queue 满了以后有几种策略：

### Strategy A

阻塞 Producer：

```text
Queue Full
   ↓
await queue.put()
   ↓
Producer pause
```

适用于：

> 不允许丢 Token。

---

### Strategy B

丢弃旧事件：

```text
drop oldest
```

适用于：

> 高频状态更新。

不适合 LLM Token。

---

### Strategy C

丢弃新事件：

```text
drop newest
```

同样不适合 LLM。

---

### Strategy D

断开慢 Consumer：

```text
Queue Full
   ↓
Client too slow
   ↓
disconnect
```

适合：

> 高并发系统保护。

Demo 推荐：

```text
LLM Token Stream
    ↓
bounded queue
    ↓
block producer
```

同时增加：

```text
max_queue_wait
```

超时则：

```text
cancel stream
```

---

# 14. SSE 实现

FastAPI：

```python
from fastapi.responses import StreamingResponse


@app.get("/v1/chat/stream")
async def chat_stream():

    async def generator():

        async for event in stream_manager.subscribe(
            stream_id
        ):

            yield (
                f"id: {event.seq}\n"
                f"event: {event.type}\n"
                f"data: {json.dumps(event.data)}\n\n"
            )

    return StreamingResponse(
        generator(),
        media_type="text/event-stream"
    )
```

---

# 15. SSE Header

必须设置：

```text
Content-Type:
text/event-stream
```

同时建议：

```text
Cache-Control:
no-cache

Connection:
keep-alive
```

Nginx 等反向代理还需要避免 buffering。

核心原则：

> **中间层不能把你的 Streaming Response 缓存成完整 HTTP Response。**

---

# 16. Heartbeat

长时间没有 Token 时：

```text
Client
   │
   │
   │ 10s
   │
   ▼
Gateway
```

可能被：

```text
Nginx
Load Balancer
Firewall
```

认为连接已经失效。

因此需要：

```text
heartbeat
```

例如 SSE：

```text
: heartbeat

```

这是一条 SSE comment，不会作为业务事件交给客户端。

---

# 17. WebSocket

WebSocket 是双向通信：

```text
Client ⇄ Server
```

适合：

```text
实时 Agent
语音
交互式 UI
Tool Call
实时控制
```

例如：

```python
@app.websocket("/v1/ws")
async def websocket_endpoint(websocket: WebSocket):

    await websocket.accept()

    while True:

        message = await websocket.receive_json()

        ...
```

发送：

```python
await websocket.send_json({
    "type": "token",
    "seq": seq,
    "delta": token
})
```

---

# 18. SSE vs WebSocket

| 特性        | SSE             | WebSocket |
| --------- | --------------- | --------- |
| 通信方向      | Server → Client | 双向        |
| HTTP      | 是               | Upgrade   |
| 实现复杂度     | 低               | 高         |
| 自动重连      | 浏览器支持           | 需要自己实现    |
| LLM Token | 非常适合            | 适合        |
| Tool 控制   | 一般              | 更适合       |
| 浏览器兼容     | 好               | 好         |
| Debug     | 简单              | 稍复杂       |

Demo：

> **先实现 SSE，再实现 WebSocket。**

---

# 19. 断线检测

FastAPI：

```python
if await request.is_disconnected():
    ...
```

例如：

```python
async def stream_generator():

    while True:

        if await request.is_disconnected():

            await stream_manager.cancel(
                stream_id
            )

            break

        event = await queue.get()

        yield encode(event)
```

---

# 20. 上游也必须取消

这是非常重要的一点。

错误：

```text
Client disconnect
      ↓
Gateway stop sending
      ↓
LLM Provider
      ↓
继续生成
```

这会造成：

```text
Token
 ↓
没人消费
 ↓
平台仍然付费
```

正确：

```text
Client disconnect
       ↓
Gateway detect
       ↓
cancel downstream
       ↓
cancel upstream LLM request
       ↓
finalize usage
```

即：

```text
Client
  X
  │
  ▼
Gateway
  X
  │
  ▼
LLM
```

整个链路都需要 cancellation。

---

# 21. asyncio Cancellation

推荐：

```python
task = asyncio.create_task(
    call_llm()
)

try:

    await stream(task)

except asyncio.CancelledError:

    task.cancel()

    raise
```

关键：

> **Cancellation 必须沿调用链传播。**

---

# 22. Reconnect

SSE 原生支持：

```text
Last-Event-ID
```

Server：

```text
id: 1

id: 2

id: 3
```

Client 断线。

浏览器重新请求：

```http
Last-Event-ID: 3
```

Gateway：

```text
stream_id = xxx
last_seq = 3
```

然后：

```text
replay seq > 3
```

---

# 23. Replay Buffer

因此 Gateway 不能只保存：

```text
当前 event
```

需要保存最近 N 个 Event：

```text
Stream
 ├── seq 1
 ├── seq 2
 ├── seq 3
 ├── seq 4
 ├── seq 5
 └── seq 6
```

例如：

```python
from collections import deque

events = deque(maxlen=1000)
```

这样最多保留：

```text
1000 events
```

---

# 24. Replay

```python
async def replay(
    stream_id: str,
    last_seq: int
):

    events = stream.events

    for event in events:

        if event.seq > last_seq:

            yield event
```

例如：

```text
Buffer:

1
2
3
4
5
6
7
```

Client：

```text
Last-Event-ID = 4
```

Replay：

```text
5
6
7
```

---

# 25. Replay Window

Replay Buffer 不可能永久保存。

例如：

```text
max_events = 1000
```

当前：

```text
5000
```

Buffer：

```text
4001 ~ 5000
```

如果 Client：

```text
Last-Event-ID = 100
```

那么：

```text
100
```

已经不在 Buffer 中。

此时不能假装可以恢复。

应该返回：

```text
409 Stream Resume Too Old
```

或者：

```json
{
    "error": "resume_window_expired"
}
```

然后 Client：

```text
重新请求
```

---

# 26. 部分失败

Streaming 中可能出现：

```text
token1
token2
token3
token4
   ↓
Provider timeout
```

这时候不能再返回：

```text
500 Internal Server Error
```

然后把前面 Token 全部丢掉。

应该发送：

```json
{
    "type": "error",
    "seq": 5,
    "error": {
        "code": "UPSTREAM_TIMEOUT",
        "retryable": true
    }
}
```

最后：

```json
{
    "type": "done",
    "seq": 6,
    "reason": "error"
}
```

Client 可以知道：

```text
已经生成部分结果
```

---

# 27. 部分结果状态

建议：

```text
StreamResult:

status
content
usage
error
```

例如：

```json
{
    "status": "partial",

    "content": "这是已经生成的部分内容",

    "usage": {
        "input_tokens": 100,
        "output_tokens": 200
    },

    "error": {
        "code": "UPSTREAM_TIMEOUT"
    }
}
```

---

# 28. TTFT

这是本项目最重要的指标之一。

TTFT：

> Time To First Token

定义：

```text
TTFT
=
First Token Timestamp
-
Request Accepted Timestamp
```

例如：

```text
Request:
10:00:00.000

First Token:
10:00:00.350
```

那么：

```text
TTFT = 350 ms
```

---

# 29. 为什么 TTFT 比总耗时重要？

两个模型：

### Model A

```text
TTFT = 300ms
Total = 10s
```

用户：

```text
300ms
↓
马上开始看到结果
```

---

### Model B

```text
TTFT = 5s
Total = 7s
```

用户：

```text
5s
↓
什么都没有
```

虽然：

```text
Model B Total = 7s
Model A Total = 10s
```

但是用户通常会认为：

> Model A 更快。

所以 LLM Streaming 需要关注：

```text
TTFT
TPOT
Total Latency
```

而不是只看：

```text
Average Response Time
```

---

# 30. 核心 Streaming Metrics

定义：

### TTFT

```text
Request → First Token
```

### TPOT

Time Per Output Token：

```text
Generation Time
/
Output Tokens
```

### Throughput

```text
Output Tokens
/
Second
```

### Total Latency

```text
Request → DONE
```

---

# 31. Metrics

建议：

```text
stream_requests_total

stream_completed_total

stream_failed_total

stream_cancelled_total

stream_ttft_seconds

stream_total_latency_seconds

stream_output_tokens_total

stream_disconnect_total

stream_reconnect_total

stream_replay_total

stream_backpressure_total
```

---

# 32. 一个完整请求

```text
Client
  │
  │ POST /chat
  ▼
Gateway
  │
  │ request accepted
  │
  │ t0
  ▼
LLM Provider
  │
  │
  │
  │ first token
  │ t1
  ▼
Gateway
  │
  │
  ▼
Client
```

记录：

```text
TTFT = t1 - t0
```

继续：

```text
token1
token2
token3
...
tokenN
DONE
```

记录：

```text
total_latency = done - t0
```

---

# 33. 与 TokenMeter 集成

第三个项目应该直接接入第二个项目。

Streaming：

```text
LLM
 │
 ├── token
 ├── token
 ├── token
 │
 ▼
StreamManager
 │
 ├── Client
 │
 └── TokenMeter
```

TokenMeter：

```text
input_tokens
output_tokens
```

StreamInfra：

```text
TTFT
TPOT
total latency
```

最终：

```text
Usage
{
    input_tokens,
    output_tokens,
    ttft,
    total_latency,
    status
}
```

---

# 34. 推荐的数据模型

```sql
CREATE TABLE streams (
    id UUID PRIMARY KEY,

    request_id UUID NOT NULL,

    status VARCHAR(32) NOT NULL,

    last_seq BIGINT NOT NULL DEFAULT 0,

    first_token_at TIMESTAMP,

    completed_at TIMESTAMP,

    error_code VARCHAR(128),

    created_at TIMESTAMP NOT NULL
);
```

---

# 35. Stream Events

Demo 可以先使用 Redis 或内存。

生产设计：

```sql
CREATE TABLE stream_events (
    stream_id UUID NOT NULL,

    seq BIGINT NOT NULL,

    event_type VARCHAR(32) NOT NULL,

    payload JSONB NOT NULL,

    created_at TIMESTAMP NOT NULL,

    PRIMARY KEY(stream_id, seq)
);
```

这样：

```text
stream_id + seq
```

天然唯一。

---

# 36. Redis 版本

生产环境更推荐：

```text
Redis Stream
```

例如：

```text
XADD stream:xxx *
```

事件：

```text
seq
type
payload
timestamp
```

Consumer：

```text
XREAD
```

这样可以实现：

```text
Producer
   ↓
Redis Stream
   ↓
Consumer
```

---

# 37. 为什么 Redis Stream 比 Pub/Sub 更适合

Pub/Sub：

```text
Producer
   ↓
Redis Pub/Sub
   ↓
Client
```

Client 断线：

```text
消息丢失
```

Redis Stream：

```text
Producer
   ↓
Redis Stream
   ↓
Consumer
```

消息可以保留。

因此：

> **需要 Resume / Replay 时，Stream 比 Pub/Sub 更合适。**

---

# 38. Demo 分阶段实现

不要一开始就：

```text
SSE
WebSocket
Redis
Kafka
Kubernetes
```

全部上。

推荐：

## V1：最简单 Streaming

```text
FastAPI
   ↓
StreamingResponse
   ↓
SSE
```

实现：

```text
Token → Client
```

---

## V2：Event Model

增加：

```text
StreamEvent
seq
stream_id
event_type
```

---

## V3：Backpressure

加入：

```python
asyncio.Queue(maxsize=N)
```

测试：

```text
Fast Producer
Slow Consumer
```

---

## V4：Disconnect

实现：

```text
Client disconnect
 ↓
cancel upstream
```

---

## V5：Reconnect

增加：

```text
Last-Event-ID
```

和：

```text
Replay Buffer
```

---

## V6：WebSocket

增加：

```text
/v1/ws
```

---

## V7：Redis

替换：

```text
Memory
```

为：

```text
Redis Stream
```

---

## V8：Observability

增加：

```text
TTFT
TPOT
Throughput
Reconnect
Backpressure
```

---

# 39. 项目目录

```text
streaminfra/
│
├── README.md
├── pyproject.toml
├── docker-compose.yml
│
├── streaminfra/
│   │
│   ├── api/
│   │   ├── sse.py
│   │   └── websocket.py
│   │
│   ├── core/
│   │   ├── event.py
│   │   ├── stream.py
│   │   ├── state.py
│   │   └── manager.py
│   │
│   ├── transport/
│   │   ├── sse.py
│   │   └── websocket.py
│   │
│   ├── buffer/
│   │   ├── memory.py
│   │   └── redis.py
│   │
│   ├── backpressure/
│   │   └── controller.py
│   │
│   ├── provider/
│   │   └── mock_llm.py
│   │
│   ├── metrics/
│   │   └── latency.py
│   │
│   └── main.py
│
└── tests/
    │
    ├── unit/
    │   ├── test_event.py
    │   ├── test_buffer.py
    │   └── test_backpressure.py
    │
    ├── integration/
    │   ├── test_sse.py
    │   ├── test_websocket.py
    │   └── test_reconnect.py
    │
    └── chaos/
        ├── test_disconnect.py
        ├── test_upstream_failure.py
        └── test_slow_client.py
```

---

# 40. Mock LLM

不要第一版就依赖真实 LLM。

先写：

```python
async def mock_llm():

    tokens = [
        "你好",
        "，",
        "这是",
        "一个",
        "Streaming",
        "Demo"
    ]

    for token in tokens:

        await asyncio.sleep(0.1)

        yield token
```

这样可以稳定测试：

```text
TTFT
Backpressure
Disconnect
Reconnect
Failure
```

---

# 41. SSE Demo

核心逻辑：

```python
async def stream():

    seq = 0

    async for token in mock_llm():

        seq += 1

        event = StreamEvent(
            stream_id=stream_id,
            seq=seq,
            type="token",
            data={
                "delta": token
            }
        )

        await queue.put(event)

        yield encode_sse(event)

    yield encode_done(seq)
```

---

# 42. 一个重要的工程问题：不能只 yield

很多初学者会写：

```python
async def stream():

    async for token in llm():

        yield token
```

这能跑。

但它缺少：

```text
stream_id
seq
buffer
replay
backpressure
metrics
disconnect
cancellation
error
```

因此：

> **它是 Streaming API，不是 Streaming Infrastructure。**

你这个项目真正要练的是后者。

---

# 43. 失败恢复

完整链路：

```text
Client
  │
  ▼
Gateway
  │
  ▼
Provider
  │
  ├── token 1
  ├── token 2
  ├── token 3
  │
  X timeout
```

Gateway：

```text
token 1
token 2
token 3
error
done
```

Client：

```text
partial response
```

不要：

```text
直接 HTTP 500
```

因为 HTTP Status 在 Streaming 开始之后已经很难改变。

---

# 44. Streaming 开始后为什么不能返回 500？

HTTP：

```text
HTTP/1.1 200 OK
Content-Type: text/event-stream
```

一旦 Header 已经发出去：

```text
200 OK
```

后面：

```text
Provider failure
```

不能重新修改成：

```text
500
```

因此错误必须成为：

> **Stream Event**

例如：

```json
{
    "type": "error",
    "code": "UPSTREAM_TIMEOUT"
}
```

这是 Streaming 系统和普通 REST API 非常重要的区别。

---

# 45. 推荐统一协议

最终：

```text
START
  ↓
METADATA
  ↓
TOKEN
  ↓
TOKEN
  ↓
TOKEN
  ↓
...
  ↓
DONE
```

失败：

```text
START
  ↓
TOKEN
  ↓
TOKEN
  ↓
ERROR
  ↓
DONE
```

取消：

```text
START
  ↓
TOKEN
  ↓
TOKEN
  ↓
CANCELLED
  ↓
DONE
```

---

# 46. 最终架构

```text
                         Client
                           │
                    ┌──────┴──────┐
                    │             │
                   SSE        WebSocket
                    │             │
                    └──────┬──────┘
                           │
                           ▼
                    Stream Gateway
                           │
                           ▼
                    Stream Manager
                           │
              ┌────────────┼────────────┐
              │            │            │
              ▼            ▼            ▼
           Event        Backpressure   Metrics
           Buffer
              │
              ▼
          Redis Stream
              │
              ▼
        LLM Provider
              │
              ▼
          Token Meter
              │
              ▼
           Billing
```

---

# 47. 三个项目串起来

到这里你的三个 Demo 就形成完整的 LLM Gateway 基础设施：

```text
                  Client
                    │
                    ▼
             ┌─────────────┐
             │ OpenGateway │
             └──────┬──────┘
                    │
                    ▼
              RateLimiter
                    │
                    ▼
              Model Router
                    │
                    ▼
             StreamInfra
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
         SSE              WebSocket
          │
          ▼
      StreamManager
          │
      ┌───┴────┐
      ▼        ▼
   Buffer   Backpressure
      │
      ▼
     LLM
      │
      ▼
   TokenMeter
      │
      ▼
    Pricing
      │
      ▼
   Billing
      │
      ▼
    Stripe
```

---

# 48. 本项目真正需要掌握的问题

完成这个项目后，应该能够回答：

### Streaming

> SSE 和 WebSocket 有什么区别？为什么 LLM Token Streaming 通常可以使用 SSE？

### Backpressure

> 如果 LLM 每秒生成 100 Token，但客户端只能消费 10 Token，怎么办？

### Memory

> 为什么不能无限缓存 Streaming Event？

### Disconnect

> 用户关闭浏览器以后，Provider 还在生成怎么办？

### Cancellation

> Gateway 如何把客户端取消传播到上游 LLM？

### Reconnect

> Client 收到 Token 1~100 后断线，重连应该从哪里开始？

### Replay

> 如果客户端 Last-Event-ID 已经超出 Replay Buffer 怎么办？

### Partial Failure

> Streaming 已经返回 100 个 Token 后 Provider 发生异常，HTTP Status 应该返回多少？

### TTFT

> 为什么 Streaming 系统重点关注 TTFT，而不是平均响应时间？

### Distributed

> 如果 Gateway 有 10 个实例，Stream State 放在哪里？

### Redis

> 为什么 Redis Stream 比 Pub/Sub 更适合实现 Streaming Replay？

---

# 49. 最终 Demo 验收标准

项目完成后，用下面这个场景测试：

```text
Client
  │
  │ Request
  ▼
Gateway
  │
  ▼
Mock LLM
  │
  ├── Token 1
  ├── Token 2
  ├── Token 3
  ├── Token 4
  │
  ▼
Client disconnect
```

然后：

```text
Client reconnect
Last-Event-ID: 4
```

Gateway：

```text
Replay
Token 5
Token 6
Token 7
...
```

最终：

```text
DONE
```

同时监控：

```text
TTFT
TPOT
Total Latency
Output Tokens
Disconnect Count
Reconnect Count
Replay Count
Backpressure Count
```

如果这一整条链路能够跑通，那么这个 Demo 就不再只是一个“FastAPI StreamingResponse 示例”，而是已经具备了一个简化版 **LLM Streaming Infrastructure** 的核心骨架。
