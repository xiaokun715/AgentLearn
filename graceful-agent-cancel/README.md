# graceful-agent-cancel

> **Agent 优雅中止（Graceful Agent Cancel）Demo —— 给“等不及”的 Agent 装四道刹车**
>
> 交互层(异步+SSE+Stop按钮) / 循环层(取消令牌) / 底层(asyncio cancel) / 兜底层(回滚+Partial Yield)

对应设计说明书：`../第十章：Agent 优雅中止.md`

---

## 一句话定位

> Agent 一次任务要跑几十秒甚至几分钟（多轮 Thought + 慢速外部 API）。系统不能没有“中途停止”。
> 但停止不能只是前端一个按钮 —— 需要**四道防线**从用户点击一路穿透到正在 `await` 的网络 I/O，
> 最后还要干净利落地收拾好脏数据，并给用户一段友好的“部分结果”。

```
用户点红色 Stop
    │  ① 交互层    POST /cancel 下发 CANCEL_TASK（异步任务先拿 task_id，SSE 看进度）
    ▼
网关（Gateway）
    │  ② 循环层    在“Redis”标记 status=cancelled
    ▼
Agent Loop 下一次动作边界 → 撞上取消埋点 → 主动跳出（只拦“动作之间”）
    │  ③ 底层      对正在跑的 asyncio.Task.cancel() → 掐断卡在 20s I/O 里的协程
    ▼
Supervisor（兜底层）
    │  ④ 回滚临时表 / 删除临时文件 → 基于已收集事实输出 Partial Yield
    ▼
用户看到：友好话术 + 停止前已查到的数据（而不是冷冰冰的 “Task Cancelled” 红字）
```

---

## 四道防线 ↔ 代码对照（第十章全文拆解）

| # | 防线 | 章节要点 | 本 Demo 落地 |
|---|------|---------|-------------|
| 1 | 交互层 | 不用同步 HTTP 等 Agent；异步提交先拿 `task_id`；WebSocket/SSE 长连接看进度；红色 Stop 按钮发 `CANCEL_TASK` | `POST /v1/tasks` 立即 `202 {task_id}`；`GET /v1/tasks/{id}/events` **SSE** 实时推事件；`static/index.html` 提供红色 **STOP (CANCEL_TASK)** 控制台（`EventSource` + `POST cancel`） |
| 2 | 循环层 | Agent Loop 每次调 LLM/工具前插桩；`redis.get(status)==cancelled` 就 break | `AgentContext.check_cancel()` 在**每个 LLM/工具动作入口 + 每轮 step 前**查询取消状态；撞上抛 `AgentStopRequested`（`agent/base.py`）。取消状态存于 `CancellationStore`（内存版模拟 Redis，`store/`） |
| 3 | 底层 | `context.Context` / `asyncio.Task.cancel()` 抛取消信号，掐断 HTTP I/O，省 Token | `RunningTaskRegistry.request_cancel(mode="force")` 调 `handle.coro.cancel()`；假 LLM 逐块“吐字”、慢速 `fetch_web` 逐分片“下载”的 `await` 就是被掐断点（`cancellation/registry.py`、`agent/llm.py`、`agent/tools.py`） |
| 4 | 兜底层 | 捕捉 `CancelledError` → 事务回滚 / 删临时文件；Partial Yield 给友好快照 | `TaskSupervisor` 统一接住 `AgentStopRequested` / `asyncio.CancelledError` / 异常：`SimLedger.rollback()`（临时表回滚）+ `ReportWorkspace.cleanup()`（删临时文件），再 `build_partial()` 输出“已为您查到……剩余操作已取消”（`agent/supervisor.py`、`resources/`、`agent/artifacts.py`） |

### cooperative vs force —— 两条取消路径的区别（演示的核心）

| | `mode="cooperative"`（仅层2） | `mode="force"`（层2 + 层3） |
|---|---|---|
| 做什么 | 只把取消状态标记为 cancelled | 标记 cancelled **并且** 对正在跑的 `asyncio.Task.cancel()` |
| 何时停下 | **下一个动作边界**（下次调 LLM/工具前） | **当场**，CancelledError 抛进正在 await 的那一行 |
| 卡在 20s I/O 里 | 要等它跑完才停（浪费 Token） | 立刻掐断，省 Token |
| 事件 | `LOOP_CANCEL_BREAK` | `FORCE_CANCELLED` |

> 生产里的“Stop 按钮”通常 = **force**（既拦动作边界、又掐 I/O）。
> `cooperative` 专门用来让你只观察层2 这一条链路。

---

## 快速开始（零外部依赖，纯 Python）

```bash
# 安装（FastAPI + uvicorn + pydantic）
cd graceful-agent-cancel
pip install -e ".[test]"

# 方式一：跑带红色 Stop 按钮的实时控制台
python examples/live_server.py
#   浏览器打开 http://localhost:8000/
#   提交任务 -> SSE 实时滚动 -> 随时点红色 STOP，看它被四道防线优雅叫停
```

三个核心 API：

```bash
# 1) 异步提交 —— 立即拿到 task_id（HTTP 请求不会等 Agent 跑完）
curl -X POST http://localhost:8000/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"query":"查最近2天账单和上海天气，再抓网页核对出简报"}'
# → {"task_id":"task_xxx","status":"running","events_url":...,"cancel_url":...}

# 2) SSE 实时监听每一步 / 工具调用 / 取消链路（curl 会一直流式输出）
curl -N http://localhost:8000/v1/tasks/task_xxx/events

# 3) 红色 Stop：force = 掐断 I/O（层2+层3）；cooperative = 只标记，下个动作边界跳出（仅层2）
curl -X POST http://localhost:8000/v1/tasks/task_xxx/cancel \
  -H "Content-Type: application/json" -d '{"mode":"force"}'
# → {"note":"已标记 cancelled 并向正在执行的协程抛出取消信号（asyncio.Task.cancel）..."}
```

事件历史（JSON，给轮询型客户端）与健康检查：

```bash
curl http://localhost:8000/v1/tasks/task_xxx/history   # 事件序列
curl http://localhost:8000/healthz
```

跑三个 CLI 场景（把“慢”调到合适大小，几秒演示完）：

```bash
python examples/demo_1_cooperative.py   # 层2：两次动作之间协作式取消，最贵的抓网页从未被启动
python examples/demo_2_force_cancel.py  # 层3：卡在 5s 慢 I/O 里，force 按下即停（0.00s）
python examples/demo_3_partial_yield.py # 层4：写库写到一半被取消 -> 回滚 1 行 + 删临时文件 + Partial
```

跑测试：

```bash
python -m pytest tests/ -q    # 23 passed
```

---

## 目录结构

```
graceful-agent-cancel/
├── app/
│   ├── domain/           # task(AgentTask 快照) / events(TaskEvent+EventBus) / exceptions
│   ├── store/            # CancellationStore —— 内存 dict 模拟 Redis 的 cancelled 标记
│   ├── cancellation/     # RunningTaskRegistry —— cooperative 标记 + force asyncio.Task.cancel()
│   ├── agent/            # base(AgentLoop+AgentContext 埋点) / llm(可打断的假流式LLM)
│   │                     # tools(快工具+慢速爬取) / research_agent / supervisor(兜底) / artifacts(Partial)
│   ├── resources/        # SimLedger(临时表,可回滚) / ReportWorkspace(临时文件,可清理)
│   ├── api/              # tasks(提交/查询/取消) / sse(实时流) / ui / schemas
│   ├── service.py        # TaskService —— 提交即 spawn asyncio.Task，取消走 层2/层3
│   ├── factory.py        # Runtime 装配
│   ├── config.py         # 所有“慢”都可配置，方便 demo/测试调小
│   └── main.py           # FastAPI 入口
├── static/index.html     # 红色 Stop 按钮控制台（EventSource + CANCEL_TASK）
├── examples/             # demo_1/2/3 + live_server
├── tests/                # 23 tests
└── pyproject.toml
```

---

## 最有意思的一条链路：demo_2 —— 底层切断慢速 I/O

```text
用户点 STOP ──► POST cancel mode=force
      ▼ ① 网关记事件 CANCELLATION_REQUESTED
      ▼ ② store 标记 cancelled（层2 素材）
      ▼ ③ registry: handle.coro.cancel()
      ▼ ④ CancelledError 抛进正在 await 的那一行（fetch_web 第 3/20 分片下载中）
      ▼ ⑤ supervisor 捕获 FORCE_CANCELLED
      ▼ ⑥ 回滚临时表 + 删除临时文件（层4）
      ▼ ⑦ build_partial() —— 基于已收集的账单/天气输出友好快照（层4）
  用户看到: "已根据您的要求中止… 已为您查到 2 条账单… 剩余操作已取消"
```

运行它会打印：按下 Stop 到停下 **0.00s**（而那个网页本来要爬 5s）——
“等不及”的 Agent，用户不需要陪它把 Token 烧完。

---

## API 一览

| 方法 | 路径 | 说明 | 对应防线 |
|---|---|---|---|
| POST | `/v1/tasks` | 异步提交，`202` 立即返回 `task_id` | ① 交互层 |
| GET | `/v1/tasks/{id}` | 任务快照（状态 / 进度 / facts / Partial Yield / 结果） | — |
| POST | `/v1/tasks/{id}/cancel` | CANCEL_TASK，body `{mode: force|cooperative}` | ①→②③ |
| GET | `/v1/tasks/{id}/events` | **SSE** 实时事件流（历史重放 + 实时广播） | ① 交互层 |
| GET | `/v1/tasks/{id}/history` | 事件历史 JSON | 可观测 |
| GET | `/ui` 、 `/` | 红色 Stop 按钮控制台 | ① 交互层 |

---

## 你真正需要搞懂的 8 个问题

1. **为什么 Agent 提交不能用同步 HTTP 等结果？**
   浏览器/网关几秒就超时，用户连 Stop 都来不及按。异步提交：先拿 `task_id`，
   再通过 SSE 长连接看进度 —— HTTP 请求生命周期早已结束，Agent 生命周期才刚刚开始。

2. **为什么“停止”还需要在循环里埋点？**
   因为 Agent 是多轮 Thought+Action 的 `while` 循环。埋点（每次调 LLM/工具前查一次取消状态）
   能让它在**刚完成手头一个小动作、准备做下一步**时主动跳出 —— 不必等一个动作结束。

3. **埋点拦得住“卡在 20 秒 I/O”里的 Agent 吗？**
   拦不住。埋点只发生在“两次动作之间”；Agent 正 `await` 一个慢速 HTTP 时，
   代码根本走不到埋点。这就是为什么必须有第 3 道防线（asyncio cancel）。

4. **`asyncio.Task.cancel()` 到底怎么“掐断”I/O？**
   它会往正在执行的协程**当前 await 的那一行**抛一个 `CancelledError`。
   假 LLM 逐块吐字、`fetch_web` 逐分片下载之间都是 `await`，CancelledError 落到那里，
   协程立即停 —— 等价于切掉对 OpenAI/外部工具的 HTTP 请求，Token 计费立刻停。

5. **为什么取消了还要“回滚”和“删临时文件”？**
   任务是被暴力叫停的，善后不能马虎。如果它正在往数据库写临时表 / 写临时报告文件，
   直接走人就会留下脏数据。supervisor 捕获中止信号后必须：临时表 → 事务回滚；
   临时文件 → 删除。

6. **Partial Yield 和“报错”有什么本质区别？**
   报错 = 用户什么都拿不到；Partial Yield = 把**已经收集到的事实**做成快照还给用户：
   “已为您查到前两天的账单……剩余操作已取消。”用户体验完全不同。

7. **`cooperative` 和 `force` 的生产用法是什么？**
   很多系统先给 `cooperative` 一个宽限期（让当前动作自然收尾、给出干净的部分结果），
   超时再升级成 `force` 掐断。本 Demo 把它们做成两个可观察的 mode，方便对照理解。

8. **怎么保证取消发生在“协程还没开始跑”时也不会死掉？**
   asyncio 对**启动前就被 cancel** 的任务会直接以 cancelled 结束、根本不执行协程体，
   supervisor 无从介入。所以 `_drive` 首行登记 `started` 标记，force 取消若发现任务未启动，
   由 service 同步完成善后 + Partial（`_finalize_prestart`）。

> **一句话面试答案**：
> 优雅中止不是给前端一个按钮就完事。我把 Agent 拆成**四道防线**：交互层用异步提交+SSE+Stop 指令；
> 循环层在每次调 LLM/工具前插桩查取消状态，让 Agent 在动作边界主动跳出；底层对正在跑的
> asyncio.Task 抛取消信号，直接掐断卡在慢速网络 I/O 里的协程；最后 supervisor 捕获中止信号，
> 回滚未提交的临时表、清理临时文件，并基于已收集的事实输出一段友好的 Partial Yield，
> 而不是甩给用户一个冷冰冰的 “Task Cancelled”。

---

## 设计要点对照（章节）

| 第十章内容 | 实现 |
|---|---|
| 交互层：异步化 + WebSocket/SSE 握手 | `POST /v1/tasks` 立即返回；`/events` SSE（历史重放+实时广播） |
| 交互层：红色“停止”按钮发 `CANCEL_TASK` | `static/index.html` + `POST /v1/tasks/{id}/cancel` |
| 循环层：网关把 `task_id` 标记 `status:cancelled` | `CancellationStore.mark_cancelled`（内存模拟 Redis） |
| 循环层：Loop 每次动作前插桩，撞上即 `break` | `AgentContext.check_cancel()` + `AgentStopRequested` |
| 底层：`asyncio.Task.cancel()` 掐断 I/O | `RunningTaskRegistry.request_cancel(mode="force")` |
| 兜底：捕捉 `CancelledError` | `TaskSupervisor.supervise()` |
| 兜底：事务回滚 / 删除临时文件 | `SimLedger.rollback()` + `ReportWorkspace.cleanup()` |
| 兜底：Partial Yield 友好快照 | `build_partial()` + `PARTIAL_YIELD` 事件 |

## 与你前面项目的组合

```text
用户 Stop ──▶ Gateway ──▶ Job(第五章 async-agent-job-queue 的 cancel)
                │
                └──▶ Agent Runtime（本 Demo 的四道防线）
                         ├── LLM Gateway（语义缓存 / 流式基础设施 / 限流）
                         └── 工具（Guardrails 中间件把关 → 沙箱执行）
```

本 Demo 的 `CancellationStore` 换成一个 Redis 实现，就是第十章讲的“分布式缓存标记 cancelled”；
把 `SimLedger / ReportWorkspace` 换成真实 DB 事务与工作目录，就是生产里的回滚与清理。
