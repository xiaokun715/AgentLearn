# prompt-config-registry — Prompt 与配置版本管理服务

> **核心思想：Immutable Version + Mutable Deployment（设计说明书 §3）**

Agent 不再写死 `SYSTEM_PROMPT / MODEL / TEMPERATURE`，而是每次运行时问一次配置服务：

```python
snapshot = await resolver.resolve(
    agent="test_case_agent",
    environment="prod",
    user_id="user_123",
)
# → {config_version, prompt, model, parameters, tools, routing}
```

这套系统回答一个关键问题：**「Agent 行为变了，到底是 Prompt / Model / Tools / Guardrails 中的哪一个变了？」**

---

## 1. 为什么需要版本管理？

假设 Agent 用 `Prompt v10 + Qwen3.5-27B + temp=0.2 + tools=v3` 上线，第二天发布 `Prompt v11`，
Tool Call 错误率 `5% → 30%`。没有版本管理时，你根本说不清**到底什么变了**。

有版本管理后，每次执行都有一个可复现的 **Execution Configuration Snapshot**：

```text
Execution #12345
  agent    : test_case_agent
  prompt   : test_case_agent:v11
  model    : qwen3.5-27b:v2
  tools    : tools:v5
  guardrail: guardrail:v3
```

> 最核心的一句话（§41）：
> **Prompt Registry 解决「存什么版本」；Deployment 解决「哪个环境用哪个版本」；
> Router 解决「这次请求用哪个版本」；Snapshot 解决「这次执行到底用了什么」；
> Audit/Observability 解决「为什么行为发生了变化」。**

---

## 2. 两个最重要的概念

### Version 不可变（Immutable Version）

```text
Prompt v1 ──修改──► 不允许！        Prompt v1 ──改内容──► Prompt v2
```

v1 一旦创建就永远存在。原因（§38）：

```text
LLM 是非确定系统
+ Prompt 是模型行为输入
+ Prompt 改变 = 系统行为可能改变
∴ 必须能精确复现「某次执行到底用了哪个 Prompt」
```

### Deployment 可变（Mutable Deployment）

```text
Production ──► v3     Production ──► v4     Production ──► 90% v3 + 10% v4
```

Deployment 只是一张**路由表**，随时可以改。发布/灰度/回滚都只改路由表，
**从不修改任何 Version 内容**。

---

## 3. 架构

```text
                        Web / CLI / curl
                              │
                              ▼
                        Config API (FastAPI)
                              │
             ┌────────────────┼─────────────────┐
             ▼                ▼                 ▼
      Prompt Registry    Config Registry    Deployment
             │                │                 │
             └────────────────┼─────────────────┘
                              ▼
                      Version Resolver
                              │
                              ▼
                         A/B Router (hash bucket)
                              │
                     ┌────────┴────────┐
                     ▼                 ▼
                 Variant A         Variant B
                     │                 │
                     └────────┬────────┘
                              ▼
                      Config Snapshot
                              │
                              ▼
                        Agent Runtime
```

旁边配：`PostgreSQL / Redis / Audit Log / Cache`。

## 4. 目录结构（§6）

```text
prompt-config-registry/
├── app/
│   ├── api/            # prompts.py / configs.py / deployments.py / resolve.py / schemas.py
│   ├── domain/         # prompt.py / config.py / deployment.py / experiment.py / audit.py / exceptions.py
│   ├── registry/       # prompt_registry.py / config_registry.py   （不可变版本 CRUD）
│   ├── router/         # hash_router.py / ab_router.py / canary.py
│   ├── resolver/       # config_resolver.py                        （运行时核心）
│   ├── deployment/     # publisher.py / rollback.py
│   ├── audit/          # audit_service.py
│   ├── cache/          # base.py / memory.py / redis_cache.py / keys.py
│   ├── storage/        # repository.py / memory.py / sqlite.py / postgres.py / models.py
│   ├── config.py       # RegistryConfig（from_env）
│   ├── factory.py      # build_runtime()
│   └── main.py         # create_app() / uvicorn 入口
├── migrations/         # PostgreSQL 建表 SQL
├── tests/              # 51 个测试用例
├── examples/           # agent.py（§1 场景）/ lifecycle.py（全生命周期）
├── experiments/        # 实验 1 A/B / 实验 2 故障回滚
└── docker-compose.yml  # PostgreSQL + Redis（可选）
```

---

## 5. 快速开始

```bash
# 安装（含测试依赖）
pip install -e ".[test]"

# 跑测试（51 个用例，默认内存后端，零依赖）
pytest

# 示例 1：Agent 不再硬编码配置（§1）
python examples/agent.py

# 示例 2：完整生命周期 创建 -> 部署 -> 灰度 -> 全量 -> 回滚
python examples/lifecycle.py

# 实验 1：A/B 测试 —— Prompt v1(简洁) vs v2(详细)（§36）
python experiments/experiment_1_ab_testing.py

# 实验 2：故障回滚 —— 指标暴跌自动回滚（§37）
python experiments/experiment_2_rollback.py

# 启动 HTTP 服务（默认 SQLite 持久化 + 内存缓存）
uvicorn app.main:app --port 8000
```

### 可选：PostgreSQL + Redis（§26~§29 / §23）

```bash
docker compose up -d          # 首次启动自动执行 migrations/001_create_tables.sql
pip install ".[postgres,redis]"
export STORAGE_BACKEND=postgres
export DATABASE_URL="postgresql://registry:registry@localhost:5432/config_registry"
export CACHE_BACKEND=redis
export REDIS_URL="redis://localhost:6379/0"
uvicorn app.main:app --port 8000
```

---

## 6. API（§30）

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/v1/prompts` | 创建 Prompt 实体 |
| POST | `/v1/prompts/{name}/versions` | 追加不可变 Prompt 版本（v=max+1） |
| GET | `/v1/prompts/{name}/versions` | 列出版本 |
| GET | `/v1/prompts/{name}/versions/{v}` | 获取单个版本 |
| POST | `/v1/agents/{agent}/configs` | 追加不可变 Config 版本 |
| GET | `/v1/agents/{agent}/configs` | 列出 Config 版本 |
| GET | `/v1/agents/{agent}/configs/{v}` | 获取单个 Config |
| POST | `/v1/deployments` | **发布**（可指定 traffic_percent 做 canary） |
| POST | `/v1/deployments/{id}/rollout` | **灰度**（调整流量百分比） |
| POST | `/v1/deployments/{id}/rollback` | **回滚**（不删除版本） |
| GET | `/v1/deployments` | 列出所有环境绑定 |
| GET | `/v1/deployments/{id}` | 查看单个（含 canary 进度） |
| GET | `/v1/resolve?agent=&environment=&user_id=` | **运行时解析**（最重要） |
| GET | `/v1/audit` | 审计日志 |
| GET | `/healthz` | 健康检查 |

### 最重要的 Resolve API（§31）

```bash
curl "http://localhost:8000/v1/resolve?agent=test_case_agent&environment=prod&user_id=user_123"
```

返回：

```json
{
  "agent": "test_case_agent",
  "config_version": 13,
  "prompt": {"name": "test_case_agent", "version": 20, "template": "..."},
  "model": {"provider": "qwen", "name": "qwen3.5-27b"},
  "parameters": {"temperature": 0.2, "max_tokens": 4096},
  "tools": {"version": 8},
  "routing": {
    "experiment": "prompt_v20_test",
    "variant": "B",
    "bucket": 42,
    "rules": [{"version": 20, "weight": 90}, {"version": 21, "weight": 10}]
  },
  "execution_identity": "test_case_agent|config:v13|prompt:test_case_agent:v20|..."
}
```

`execution_identity` 建议直接写入 Trace / Token Meter / Semantic Cache Key（§33~§35）。

---

## 7. 核心概念速览

### 7.1 Prompt 与 Config 版本（§7~§11）

- **Prompt** 是「名字」实体；**PromptVersion** 是不可变内容（template + variables + metadata）。
- **AgentConfig** 是一次性组合快照：`model + parameters + prompt 引用 + tools + guardrails`。
- 版本号自动递增（v=max+1），**重复创建同版本直接 409**（§8：版本不可变）。

### 7.2 环境绑定（§12~§13）

```text
dev ─► v15     staging ─► v14     prod ─► v12
```

同一个 (agent, environment) 组合只有**一行** Deployment —— 避免「开发者改 Prompt 直接上线」，
必须走 dev → staging → canary → prod 的发布流程。

### 7.3 Deployment = 路由表（§19~§20）

```json
{
  "agent": "test_case_agent",
  "environment": "prod",
  "status": "CANARY",
  "rules": [
    {"version": 12, "weight": 90},
    {"version": 13, "weight": 10}
  ]
}
```

`rules` 是权威路由表。回滚快照 `previous_rules` 在 publish 时定格为「发布前路由」，
rollout 不改它 —— 这样无论灰度到几，回滚都回到发布前的老版本（§18）。

### 7.4 A/B Router —— Hash 而不是 Random（§15~§16）

```python
def bucket(user_id: str) -> int:
    h = hashlib.sha256(user_id.encode()).hexdigest()
    return int(h[:8], 16) % 100
```

为什么 Hash（§16）：`random` 会让同一个用户第一次 v12、第二次 v13、第三次 v12，
实验数据非常混乱。Hash 保证 **Sticky Assignment**：同一个用户永远命中同一个版本。

> 注意：不要用 Python 内置 `hash()` —— 它每次进程启动会加随机 salt，跨重启后分组会变。

### 7.5 Canary（§17）

```text
5% → 10% → 30% → 50% → 100%
```

灰度每一步都监控 Error Rate / Latency / Token Cost / Tool Call Error / Success Rate，
异常就回滚。状态机（§14）：`STAGED → CANARY → RELEASED`。

### 7.6 Rollback（§18 / §37）

**Rollback 不是删除新版本**。v13 仍然存在、仍可随时重新部署；Rollback 只是把路由表还原。

```text
Production v13 (Tool Error 5%→20%)
    │  Rollback
    ▼
Production v12 (恢复)
```

### 7.7 缓存（§23）

两类 key：

```text
deploy:{agent}:{environment}   # 路由表（可变）→ 发布/灰度/回滚时显式失效
config:{agent}:v{version}      # 快照（版本化 key，版本不可变 → 无需失效）
```

**版本化 key 的关键收益**：发布 v13 不会把 v12 的缓存顶掉，命中率更高，
且绝不可能出现「新 Prompt + 旧答案」。

### 7.8 审计（§24~§25）

每一次创建 / 发布 / 灰度 / 回滚都写 `audit_logs`（before / after / actor / reason）。
线上行为突变时，审计日志帮你做到 **Change Attribution**：

```text
22:01  Prompt v12 正常
22:10  Prompt v13 deployed   ← 看审计日志定位到这一条
22:15  Tool error ↑
```

---

## 8. 两个实验

### 实验 1：A/B 测试（§36）

Prompt v1「请简洁回答问题」vs Prompt v2「请详细回答问题，并给出三个例子」，
Production 100%→v1，再灰度 v2 @ 10%。模拟 1000 个请求后统计：

```text
              v1(简洁)    v2(详细)
Success        83%        89%
Latency       1.29s      1.92s
Tokens         571        964
Cost         $0.0022    $0.0041
```

结论：v2 成功率更高 → 全量发布。这就是「**Configuration → Cost Attribution**」。

### 实验 2：故障回滚（§37）

Config v1 正常（Tool Success 95%）→ 发布 v2（Tool Success 跌到 68%）→
系统检测到跌破阈值 → 自动 Rollback → v1（恢复 95%），且 v2 版本仍然存在。

---

## 9. 与其他项目的关系（§32~§35）

```text
Agent Runtime
    │
    ├── Config Resolver   ← 本次项目：Prompt / Model / Tools / Deployment / A/B / Canary / Rollback
    │
    └── LLM Gateway       ← 前序项目：Auth / RateLimit / TokenMeter / Cache / Provider / Streaming
```

| 项目 | 回答的问题 |
| ---- | ---------- |
| OpenGateway | 请求应该发给谁？ |
| TokenMeter | 这个请求用了多少资源？ |
| StreamInfra | 结果如何实时、可靠地交付？ |
| SemanticCache | 这个请求是否可以根本不用调用 LLM？ |
| Async Job Queue | 长时间运行的 Agent 任务如何解耦？ |
| Tool Sandbox | 工具执行如何安全隔离？ |
| **Config Registry** | **这次执行到底用了哪个 Prompt / Model / 参数？** |

### 与 Semantic Cache 的衔接（§33）

Semantic Cache 的 Key 必须包含 `execution_identity`，否则会出现「新 Prompt + 旧答案」：

```python
semantic_cache_key = hash(
    normalized_prompt,
    prompt_version,
    model_version,
    config_version,
)
```

### 与 Token Meter 的衔接（§34）

```json
{
  "request_id": "req_001",
  "agent": "test_case_agent",
  "config_version": 13,
  "prompt_version": 20,
  "model": "qwen3.5-27b",
  "input_tokens": 1500,
  "output_tokens": 600
}
```

这样能直接分析「Prompt v12 平均 1800 tokens vs Prompt v13 平均 2400 tokens（成本 +33%）」。

---

## 10. 面试问题清单（§41）

1. **为什么 Prompt 需要版本管理？** —— LLM 非确定 + Prompt 是行为输入，必须能精确复现。
2. **为什么版本必须不可变？** —— 历史执行可 Replay / Debug / Audit / Rollback / Experiment。
3. **Prompt Version 和 Config Version 有什么区别？** —— Prompt 只管内容；Config 是引用组合快照。
4. **如何保证一次执行可 Replay？** —— 每次执行记录 Config Snapshot（§11）。
5. **A/B Router 为什么用 Hash 而不是 Random？** —— Sticky Assignment（§16）。
6. **如何保证同一用户稳定命中同一版本？** —— SHA256(user_id) → bucket（§15）。
7. **Canary 和 A/B Testing 有什么区别？** —— Canary 是「逐步放量直到 100%」；A/B 是「两版同时对比」。
8. **如何设计 Rollback？** —— 路由表还原 previous_rules，不删版本（§18）。
9. **Rollback 为什么不是删除新版本？** —— 版本不可变，v13 随时可重新发布。
10. **如何避免 Config Cache 不一致？** —— 路由表显式失效 + 快照用版本化 key（§23）。
11. **Prompt 更新后 Semantic Cache 怎么处理？** —— Key 包含 prompt/model/config 版本（§33）。
12. **如何分析某版本 Token 成本？** —— Token Meter 带 config_version（§34）。
13. **如何知道 Agent 行为变化是哪个组件导致？** —— 版本化 + 审计（§25 / §35）。
14. **如何设计 Configuration Snapshot？** —— 展开后的可执行对象 + execution_identity（§11）。
15. **如何保证线上变更可审计？** —— audit_logs before/after（§24）。

---

## 11. 存储后端

| 后端 | 依赖 | 用途 |
| ---- | ---- | ---- |
| memory | 无 | 测试 / 快速 Demo |
| sqlite（默认） | `[sqlite]` | 本地持久化，进程重启数据不丢 |
| postgres | `[postgres]` | 生产，见 migrations/001_create_tables.sql |

| 缓存 | 依赖 | 用途 |
| ---- | ---- | ---- |
| memory（默认） | 无 | 单实例 Demo |
| redis | `[redis]` | 多实例共享 + 发布失效 |

切换：环境变量 `STORAGE_BACKEND` / `DATABASE_URL` / `CACHE_BACKEND` / `REDIS_URL`（见 §5）。
