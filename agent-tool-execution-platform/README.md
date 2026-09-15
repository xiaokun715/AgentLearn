# agent-tool-execution-platform

> **可靠工具执行平台（Reliable Tool Execution Platform）Demo —— 让 Agent 只负责「调用什么」，平台负责「安全、可靠、可恢复地执行」**
>
> Tool Gateway ＋ 幂等四分支 ＋ 租约/心跳 ＋ 沙箱 ＋ LangGraph Checkpoint ＋ Supervisor 式崩溃恢复
> ＋ 参数自愈 ＋ 注入防护 ＋ 重复/循环检测 ＋ 人工介入

对应设计说明书：`../第十二章：可靠工具执行系统设计说明书.md`

---

## 一句话定位

> Agent 调外部工具这件事，**失败模式比功能点重要得多**。一次 100MB 的返回值会撑爆 Context；
> 一次网络抖动可能让同一个副作用发生两次；一个崩溃的 Agent 重启后不知道自己走到哪一步；
> 一个卡在 `search → search → search` 的死循环能烧掉一整天的额度。
>
> 本 Demo 把这些全部收进一个**独立于 Agent 的平台**：Agent 只提交标准化的 `ToolCall`，
> 平台用 **Schema+Policy+Sandbox 三层防护**决定「能不能执行」，用 **幂等键 + 分布式锁**
> 决定「该不该执行」，用 **租约 + 心跳**决定「谁在执行」，用 **Checkpoint + 幂等键**决定
> 「崩溃后从哪继续」，用 **错误分类 + 恢复策略**决定「失败了往哪走」。

```
                          Agent（LangGraph）
                                │  ToolCall（不直接调 Python 函数）
                                ▼
        ┌─────────────────── Tool Gateway ────────────────────┐
        │ ① Registry  ② 重复/循环检测  ③ Schema 校验 + 参数自愈  │
        │ ④ 注入检测  ⑤ 权限校验       ⑥ 风险检查（HIGH→人工）    │
        │ ⑦ 幂等四分支：NOT_FOUND / PROCESSING / SUCCESS / FAILED│
        └──────────────────────────┬──────────────────────────┘
                                   ▼
                            Tool Scheduler
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
              Sync Executor              Async Executor
              （Agent 直接等）          （Redis Stream → job_id）
                     │                           │
                     │                    ┌──────▼──────┐
                     │                    │   Worker    │ 抢租约 → 起心跳
                     │                    └──────┬──────┘   （CAS 续租）
                     └─────────────┬─────────────┘
                                   ▼
                         Sandbox（CPU/内存/磁盘/进程/网络/超时）
                                   ▼
                              Tool Process
                                   ▼
                     Result Processor（§37-40）
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
              inline（小结果进 Context）   artifact（大结果进对象存储 + preview）
                     └─────────────┬─────────────┘
                                   ▼
                     PostgreSQL 事务：结果 + 状态 + 审计 + Outbox
                                   ▼
                     Agent Checkpoint → Resume
```

---

## 特性一览

| 主题 | 落地 |
|---|---|
| **Gateway 七道关** | `gateway/gateway.py` 一个 `submit()` 串起 §54 的完整链路；顺序刻意安排（权限在幂等前、自愈在注入前），每处都在 docstring 里写了「为什么是这个顺序」 |
| **幂等四分支** | `idempotency/manager.py` 的 `decide()` 逐分支实现 §9~§13：不存在→重提交、PROCESSING→看租约、SUCCESS→复用结果绝不重跑、FAILED→错误分类后再定 |
| **稳定的幂等键** | `domain/models.py` 的 `compute_idempotency_key()` = `tenant + workflow_run + logical_step + tool + 规范化参数`。规范化保证 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 得到同一个键 |
| **分布式锁** | `idempotency/lock.py`：`SET NX EX` 抢锁 + **释放时校验持有者**（脚本原子），避免「A 超时释放、B 拿到锁、A 迟到裸 DEL 删掉 B 的锁」 |
| **租约 + 心跳** | `lease/manager.py` 的 `renew()` 是 **compare-and-renew**（只有当前 `lease_id` 能续）；`lease/heartbeat.py` 区分 Worker 心跳与 Agent 心跳 |
| **租约回收** | `lease/reaper.py` 实现 §56：**「租约过期」只证明没人续租，不证明没在执行** —— 按 §57 幂等性等级 + §51 风险等级决定接管还是转人工 |
| **分布式安全** | 单一进程内用同步 `RedisSim`，**每个命令天然原子**，与真机「单线程串行执行」语义一致；所有状态迁移收敛到注册脚本里 |
| **三层超时** | `sandbox/policy.py` 的 `validate_timeout_layers()` + `factory.py` 的启动自检：`Tool < Sandbox < Lease`，配反了启动就告警 |
| **参数自愈** | `gateway/validation.py`：`"300"→300`、`"TC001"→["TC001"]`、丢未知字段；**只改形状不改语义**，`repair_forbidden` 字段永不放大数值（§23） |
| **注入防护** | `gateway/injection.py`：路径穿越（含 `%2e%2e%2f`/UNC/NUL 变体）、命令注入、SQL 注入、SSRF（含云元数据地址）、Prompt 注入；**按字段语义圈定适用范围**（见下） |
| **权限校验** | `gateway/permission.py`：`principal → action` 支持通配符；失败**直接拒绝不 retry**（§26） |
| **沙箱** | `sandbox/`：`auto → docker → process` 自动退化；超时真杀进程树、`kill` 幂等、环境变量白名单（不继承宿主密钥） |
| **大结果处理** | `result/processor.py`：`≤32KB` 进 Context，超过落对象存储只回 `artifact_id + preview`；实测 **608KB → 16.8KB** 视图 |
| **事务化落库** | `infra/database.py` 的 `finalize_success()`：结果 + 状态 + 审计 + **Outbox** 同事务提交（§45/§46），杜绝「Redis 说成功、DB 查不到」 |
| **错误分类与恢复** | `recovery/`：`ErrorClassifier` 九类错因 → `RecoveryPolicyEngine` 查表得动作 → **两道防无限重试的闸门**（次数上限 + 高风险强制转人工） |
| **退避重试** | `recovery/retry.py`：指数退避 + **可注入随机源**（否则退避不可复现，演示没法看）；不原地 sleep，而是进延迟队列（zset） |
| **重复 / 循环检测** | `loop/`：四要素签名（tool+参数+步骤+时间窗）逐级升级 `OK→WARNING→DEGRADED→STOP`，并给出**可执行的降级建议**（改 query / 降 top_k / 换 keyword） |
| **人工介入** | `human/approval.py`：`WAITING_HUMAN → APPROVE/REJECT/MODIFY` 状态机；**MODIFY 后必须重新校验**（人也会写错参数） |
| **LangGraph 集成** | `agent/`：Tool Node 只 `gateway.submit()` 从不直接执行；异步 Tool 走 `interrupt()`；崩溃后读 Checkpoint → 查幂等 → 复用结果 |
| **可观测性** | `observability/`：§70 的 14 个指标一个不少、Span 链路、`execution_event` 审计流（28 种事件类型） |
| **零外部依赖** | Redis / PostgreSQL / MinIO 都有同进程等价替身（`infra/`）；克隆下来直接 `python -m app.main` 就能跑通全部场景 |

---

## 快速开始

```bash
cd agent-tool-execution-platform

# 看有哪些场景
python -m app.main --list

# 全跑（约 5 秒，含 20 项故障测试）
python -m app.main all

# 只跑关心的（可选：sync async agent-crash worker-crash repair loop human security tests）
python -m app.main sync human
python -m app.main tests          # §71 的十个故障测试，带断言，退出码即结果

# 看平台内部日志（沙箱退化、配置自检、租约回收…）
python -m app.main all --verbose
```

依赖只有三个：`pydantic` / `pyyaml` / `langgraph`。**不需要** Docker、Redis、PostgreSQL、MinIO。

```bash
pip install -e .        # 或 pip install pydantic pyyaml langgraph
```

想要接真机（说明书 §69 的技术栈），`docker compose up -d` 起 PG + Redis + MinIO，
再改 `app/infra/` 下的三个实现类即可 —— 上层代码一行不用动。
这就是 §44「Redis 是协调层、PostgreSQL 是事实来源」这条边界切分带来的好处。

---

## 场景演示（§61~§67）

| 命令 | 场景 | 看什么 |
|---|---|---|
| `sync` | §61 同步 Tool | `12345 * 6789 → 83810205`；**同一调用再提交一次 → `DEDUPLICATED`，工具没有重跑** |
| `async` | §62 异步 Tool | 提交立刻拿 `job_id` → 队列深度 → Worker 消费 → 终态；注意「Stream 累计条目」不会下降，别当队列长度看 |
| `agent-crash` | §63 Agent 崩溃 | 提交 → interrupt → Agent 进程消失 → Tool 照样跑完 → 重启读 Checkpoint → **复用结果，不重跑** |
| `worker-crash` | §64 Worker 崩溃 | 抢租约 → 拨快时钟让租约过期 → Reaper 按幂等性等级决定 `RETRY` 还是 `HUMAN` |
| `repair` | §65 参数自愈 | `{"timeout":"300","test_cases":"TC001"}` → `{300, ["TC001"]}`；而 `timeout=300000` 被拒 |
| `loop` | §66 循环检测 | 相同调用连续 6 次，信号 `OK→WARNING→DEGRADED→STOP`；`A→B→A→B` 识别出周期 3 |
| `human` | §67 人工介入 | 高风险 `database_delete` → `WAITING_HUMAN` → 批准 → 执行；驳回 → `CANCELLED` |
| `security` | §24~§26 / §39/§40 | 三种注入全拦、权限不足不重试、608KB 结果压成 16.8KB |
| `tests` | §71 | 20 项断言，覆盖十个故障场景 |

### §71 故障测试结果（实测）

```
[Test 1] 两个 Agent 同时调用同一个 Idempotency Key   ✓ 执行记录数=1  ✓ 同一个 call_id
[Test 2] Worker Crash                               ✓ Lease Expire -> RETRY  ✓ 接管后完成
[Test 3] Agent Crash                                ✓ Checkpoint Resume  ✓ 没有重复执行
[Test 4] Tool Timeout                               ✓ Sandbox Kill          ✓ error_type=timeout
[Test 5] Tool 返回大结果                             ✓ artifact  608,229 -> 16,873 字节
[Test 6] Agent 连续调用同一 Tool                     ✓ OK→WARNING→DEGRADED→STOP
[Test 7] A -> B -> A -> B                           ✓ 循环长度=3 ['search','analyze','execute']
[Test 8] Permission Denied                          ✓ 拒绝且 resume_hint=ABORT（不 retry）
[Test 9] 参数类型错误                                ✓ Repair -> Validate -> Execute
[Test 10] 高风险 Tool                                ✓ WAITING_HUMAN，没人批准就不执行

§71 故障测试结果：20/20 通过
```

---

## 章节 ↔ 代码文件对照

| 说明书章节 | 本 Demo 落地 |
|---|---|
| §1~§2 职责边界、总体设计原则 | `factory.py` 的装配顺序就是一张依赖图；各模块 docstring 首段都写明边界 |
| §3 总体架构 | `gateway/gateway.py` 顶部 ASCII 图 + `main.py` 的九条场景链路 |
| §4.1 / §4.2 同步 / 异步 Tool | `scheduler/scheduler.py` 按声明的 `execution_mode` 分流（不是「跑起来看快不快」） |
| §5 Tool Metadata | `domain/models.py::ToolMetadata`，`configs/tools.yaml` 可覆盖 |
| §6 Tool Call 数据模型 | `domain/models.py::ToolCall`（含 `ensure_idempotency_key()` / `signature()`） |
| §7 Idempotency Key | `domain/models.py::compute_idempotency_key()` —— 四个分量 + 规范化，**不能只 hash 参数** |
| §8 Idempotency 状态机 | `domain/enums.py::IdempotencyStatus` + `domain/models.py::IdempotencyRecord` |
| §9~§13 幂等四分支 / 原子创建 / Recovery | `idempotency/manager.py::decide()`（四分支逐条实现）、`idempotency/state.py`（`SET NX EX` + CAS 迁移） |
| §9 Double Check | `idempotency/lock.py` + `claim` 后的回读（失败也要 GET 一次，才能区分 PROCESSING 与 SUCCESS） |
| §14 租约 | `lease/manager.py`（`lease:{call_id}` + zset 过期索引） |
| §15 Heartbeat / compare-and-renew | `lease/heartbeat.py::WorkerHeartbeat` + `lease_renew` 脚本校验 `lease_id` |
| §16~§17 Agent 心跳与生命周期 | `lease/heartbeat.py::AgentHeartbeat`（`agent_heartbeat:{agent}:{run}`） |
| §18 Checkpoint | `agent/checkpoint.py::CheckpointManager`（SqliteSaver + 每步快照） |
| §19 Agent Crash Recovery | `agent/recovery.py::plan_resume()` 逐分支实现 §19 的分支图 |
| §20 为什么 Checkpoint + Idempotency 必须一起用 | `agent/recovery.py` 模块 docstring 里的对照表 |
| §21~§23 参数自愈与限制 | `gateway/validation.py`（确定性修复为主、LLM 修复为兜底且必须再校验） |
| §24~§25 注入防护与参数安全策略 | `gateway/injection.py` + `configs/tools.yaml` 的 `params:` 段 |
| §26 Permission Check | `gateway/permission.py`（通配符 + `check_tool()`） |
| §27~§28 Sandbox | `sandbox/policy.py` / `process.py` / `docker.py` / `manager.py` |
| §29 三层超时 | `sandbox/policy.py::validate_timeout_layers()` + `factory.py::check_config()` 启动自检 |
| §30~§33 重复检测 / DAG 循环 / 降级 | `loop/duplicate.py`、`loop/cycle.py`（含可执行降级建议） |
| §34 Error Classification | `recovery/classifier.py`（九类 + 从错误文本反推，覆盖 429/503/timeout…） |
| §35 Recovery Policy | `recovery/policy.py`（查表 + 规则 A/B/C + 次数闸门） |
| §36 Retry Policy | `recovery/retry.py`（指数退避 + 可注入 jitter 源） |
| §37~§40 结果处理与截断 | `result/processor.py`、`result/artifact.py`、`result/store.py` |
| §41 Tool Execution 状态机 | `domain/enums.py::ExecutionStatus`（含 `is_terminal` / `is_pending`） |
| §42 数据库设计 | `migrations/001_init.sql`（生产 PG DDL）+ `infra/database.py`（SQLite 等价 schema） |
| §43 Redis 数据结构 | `infra/redis.py` 底部键名函数 + 各子系统 docstring |
| §44~§46 一致性 / 事务 / Outbox | `infra/database.py::finalize_success()`、`gateway.publish_outbox()` |
| §47 Agent State | `agent/state.py::AgentState` |
| §48 Tool Node | `agent/graph.py` 的 `_tool_node`（**只 submit，从不直接 run**） |
| §49 Async Tool 与 Interrupt | `agent/graph.py`（`interrupt()` + `graph.update_state` 补写 Checkpoint） |
| §50 Tool Callback | `gateway.on_completion()` + `agent/graph.py::resume()` |
| §51~§53 Human-in-the-loop | `human/approval.py` + `gateway.approve/reject/modify` |
| §54 完整执行流程 | `gateway/gateway.py::submit()` 的七步注释 |
| §55~§57 Crash 恢复 / 幂等性等级 | `agent/recovery.py`、`lease/reaper.py`、`domain/enums.py::IdempotencyLevel` |
| §58 Tool Execution API | （本 Demo 未实现 HTTP 层，入口见 `app/main.py`） |
| §59 Tool Registry | `tools/registry.py`（代码声明默认值 + YAML 覆盖） |
| §60 推荐 Demo Tool | `tools/` 下六个 Tool + 一个 fallback 替代 |
| §61~§67 七个 Demo 场景 | `app/main.py` 的九条场景 |
| §68 项目目录 | 见下 |
| §69 推荐技术栈 | `pyproject.toml` + `docker-compose.yml` |
| §70 可观测性 | `observability/metrics.py`（14 指标）、`tracing.py`、`audit.py`（28 事件） |
| §71 最重要的测试 | `python -m app.main tests` |

---

## Tool 能力矩阵（§60 的五个 + 两个必要的补充）

| Tool | 模式 | 覆盖的可靠性主题 |
|---|---|---|
| `calculator` | sync / 50ms | 纯函数、AST 安全求值（**不用 `eval`**）、幂等命中 |
| `search_knowledge` | sync / 2s | 同步 Tool、`semantic`/`keyword` 双模式（§33 降级有落点）、循环检测 |
| `run_test` | async / 长任务 | 队列、Checkpoint、Agent 崩溃恢复、进度上报 |
| `run_test_dry_run` | sync | **§35 FALLBACK 的落点** —— 没它，「换个等价 Tool 再试」会变成 `ToolNotFound` |
| `execute_python` | async / sandbox | 真沙箱执行、超时杀进程树、资源限制、环境变量白名单 |
| `large_file_analysis` | async / artifact | 大结果 → 对象存储 + preview（§39/§40） |
| `database_delete` | async / **HIGH** | 人工审批（§67）、`NON_IDEMPOTENT` 不允许自动接管（§57） |

---

## 关键设计取舍（这些是面试会追问的地方）

### 1. 平台核心是**同步**的，不是 async/await

说明书里的「异步 Tool」指的是**执行模型**（提交后返回 `job_id`、Agent 放手），
而不是 Python 的 `async def`。本 Demo 把这一点做实了：

* 调度、Worker、幂等、租约全部同步；
* 队列用 Redis Stream 的**消费者组**，Worker 是独立线程；
* 好处是 `RedisSim` 里每个命令**天然原子**（单线程内同步方法之间不会发生协程切换），
  与真机「Redis 单线程串行执行命令」的模型一致 —— 所以这里验证过的 CAS/`SET NX` 语义，
  换到真 Redis 上依然成立。

一个诚实的边界：真机上「客户端每次读都可能拿到过期视图」，而模拟器同进程共享内存。
但所有**关键状态迁移**都收敛到了脚本里（`lease_renew` / `idempotency_transition` /
`lock_release`），只要不绕开脚本直接读改写，行为就对得上。

### 2. 认领宽限期：给 §11 的分支图补一个隐含前提

§11 的分支图是 `PROCESSING → 看租约`，隐含「PROCESSING 必定有租约持有者」。
这在同步路径成立（submit 当场抢租约），**在异步路径不成立**：
Gateway 认领幂等键时任务还只是躺在队列里，抢租约的是稍后来的 Worker。

于是出现一个「已认领、尚无租约」的窗口（长度 = 排队时间）。窗口里来第二个相同幂等键的
请求，按朴素逻辑会判「执行者已死 → 可重跑」——**恰好击穿幂等**。
本 Demo 加了一层时间闸门（`idempotency_claim_grace_seconds`）：
宽限期内一律 `WAIT`，过了宽限期才进入 §56 的接管判断。
**让「等一等」成为默认，「重跑」成为需要证据的例外。**

### 3. 注入检测按**字段语义**圈定范围

SQL 注入的特征串（`sleep(`、`--`、`; DROP`）在别的语言里是**正当语法**。
全域扫描会把 `execute_python.code = "import time; time.sleep(120)"` 判成时间盲注。

**误报和漏报一样有害**：一次误报就足以让人关掉整个 `injection_check`，那才是真漏防。
所以和 `_COMMANDISH_TOKENS` / `_HOSTISH_TOKENS` 用同一条思路：
只在该语义的字段上启用该语义的规则（`_SQLISH_TOKENS`）。

### 4. 恢复路径上也要重新认领幂等键

Reaper 判定 Worker 死亡时会 `release_claim` 归还执行权，好让接管者能重新 `SET NX`。
但如果接管者只是执行、不重新认领，执行完 `complete_success` 就会去迁移一条
**已经不存在的记录** —— 执行照常成功，但幂等键消失了，**下一次相同调用会被当成
全新请求再跑一遍**。

这个 bug 只在「恢复路径」上出现，也就是系统本来就已经在故障中的时候 —— 最难发现的一类。
修法是给 Store 加一个语义明确的方法：`ensure_owned()`（不存在就认领 / PROCESSING 就回填 /
FAILED 就重认 / **SUCCESS 就拒绝**）。

### 5. 三层超时的顺序是硬约束，所以放进启动自检

§29 要求 `Tool Timeout < Sandbox Timeout < Worker Lease Timeout`。配反了平时看不出来，
只有 Tool 跑超时那一刻才会发现「沙箱抢先把进程杀了」，
于是错误类型从 `TIMEOUT` 退化成 `SANDBOX_ERROR` —— Recovery Policy 表里
`TIMEOUT` 那一行的处置（Retry / Increase timeout）**根本不会被执行**。

所以 `factory.check_config()` 在装配时会校验全部 Tool 的三层顺序、
`fallback_tool` 是否真的注册了、以及「异步 + 非幂等 + 非高风险」这种危险组合。
**这类配置层面的静默炸弹，值得用几十毫秒的启动时间换掉。**

### 6. 高风险审批之后不能「保留原样重提交」

`gateway.approve()` 重建 `ToolCall` 必须带上原始 `agent_id` / `run_id`。
不带会发生什么：批准之后用默认身份 `agent_01` 去执行 —— 要么被权限层拦下
（**批了却跑不起来**），要么更糟：用一个权限更大的默认身份跑起来，**绕过了原本的身份约束**。
所以 `ApprovalRecord` 把这些字段作为一等公民持久化，并提供 `rebuild_call()`。

### 7. 幂等键的 TTL 必须长于崩溃恢复耗时

`idempotency_ttl_seconds` 默认 24 小时。这条不是「顺手设大一点」，
而是**恢复流程正确性的一部分**：§19 的分支图里，`NOT_FOUND` 的分支会判定
「之前根本没成功提交，可以重新提交」。如果幂等键先于恢复流程过期，
一个已经跑完的非幂等操作会被当成没做过而重跑 —— 而这个误判**无法从数据上区分**。

---

## 项目目录

```
agent-tool-execution-platform/
├── app/
│   ├── main.py                 # CLI 入口：九条场景 + §71 二十项故障测试
│   ├── config.py               # 三层配置合并（代码默认 < YAML < 环境变量）
│   ├── factory.py              # 依赖装配 + 启动配置自检
│   │
│   ├── domain/                 # 共享契约（其它模块都只依赖它）
│   │   ├── enums.py            #   状态机 / 错误类型 / 幂等性等级 / 循环信号
│   │   ├── models.py           #   §5 §6 §7 §42 的全部数据模型
│   │   ├── errors.py           #   异常自带 ErrorType 分类
│   │   └── policy.py           #   参数策略 / 沙箱策略 / 重试策略 / 恢复策略表
│   │
│   ├── infra/                  # 外部依赖的同进程替身
│   │   ├── redis.py            #   SET NX EX / TTL / Stream 消费者组 / 注册脚本
│   │   ├── database.py         #   §42 四张表 + 真事务 + Outbox
│   │   ├── object_store.py     #   §39 大结果存储（MinIO 语义）
│   │   └── clock.py            #   可注入时钟（让「租约过期」不用真等）
│   │
│   ├── gateway/                # 门面与安全检查
│   │   ├── gateway.py          #   §54 七步流程
│   │   ├── validation.py       #   §21-23 校验 + 参数自愈
│   │   ├── injection.py        #   §24-25 五类注入检测
│   │   └── permission.py       #   §26 权限校验
│   │
│   ├── scheduler/              # 执行调度
│   │   ├── scheduler.py        #   §4 sync/async 分流 + Worker 池
│   │   ├── sync_executor.py    #   Executor（跑一次） + SyncExecutor（带重试）
│   │   ├── async_executor.py   #   §4.2 入队 + 延迟队列
│   │   └── worker.py           #   §56 消费 + 心跳 + 回收接管
│   │
│   ├── idempotency/            # §7-13
│   │   ├── state.py            #   状态读写 + ensure_owned + CAS 迁移
│   │   ├── lock.py             #   分布式锁（释放校验持有者）
│   │   └── manager.py          #   四分支裁决 decide()
│   │
│   ├── lease/                  # §14-17 / §56
│   │   ├── manager.py          #   租约 + compare-and-renew
│   │   ├── heartbeat.py        #   Worker 心跳 / Agent 心跳
│   │   └── reaper.py           #   过期租约回收 + 接管判定
│   │
│   ├── sandbox/                # §27-29
│   ├── tools/                  # §59-60 Registry + 六个 Tool
│   ├── result/                 # §37-40 结果处理 / artifact / 事务化持久化
│   ├── loop/                   # §30-33 重复检测 / 循环检测
│   ├── recovery/               # §34-36 分类 / 策略 / 退避
│   ├── human/                  # §51-53 审批状态机
│   ├── observability/          # §70 指标 / Span / 审计
│   └── agent/                  # §18-20 §47-50 LangGraph 集成与恢复
│
├── configs/
│   ├── tools.yaml              # §59 Tool 声明 + §25 逐参数安全策略 + §26 权限表
│   ├── sandbox.yaml            # §27 资源策略 + §29 三层超时
│   └── recovery.yaml           # §35 恢复策略表 + §36 退避 + §30/§31 循环阈值
│
├── migrations/001_init.sql     # §42 生产 PostgreSQL DDL
├── docker-compose.yml          # §69 接真机（PG + Redis + MinIO）
└── pyproject.toml
```

---

## 已知边界（说明书本身也承认的部分）

* **真正的 exactly-once 做不到**。说明书 §72 Phase 2 的原话是「尽量实现
  exactly-once effect，对于外部非幂等副作用依赖业务幂等键或人工确认」。
  本 Demo 严格遵循这条：`NON_IDEMPOTENT` 的 Tool 在租约过期后**一律转人工**，
  绝不自作主张接管重跑。
* **没有 HTTP API 层**（§58）。入口是 CLI。理由是本书考察的重点是可靠性语义
  而不是接口形态 —— 套一层 FastAPI 只会让人误以为「接口通了 == 平台对了」。
* **Sandbox 的 process 后端隔离强度弱于容器**。Docker 不可用时自动退化并打 warning；
  Windows 上 `setrlimit` 不受支持，会在 `ExecResult.meta` 里标注降级，而不是静默假装生效。
* **Worker 是线程而非独立进程**。这让「Worker 崩溃」只能被模拟（场景四就是这么做的）：
  平台侧的租约、PEL、接管逻辑是真实运行的，但进程真的被 kill 掉这一环需要部署形态配合。
