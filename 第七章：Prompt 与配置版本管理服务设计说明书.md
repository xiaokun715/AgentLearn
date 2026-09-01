# 1. 项目定位

项目名称：

```text
prompt-config-registry/
```

目标：

实现一个生产级 Prompt / Agent Config 管理 Demo，支持：

```text
Prompt 创建
Prompt 版本
Model Config
Tool Config
Environment Binding
发布
灰度
A/B
回滚
审计
运行时解析
```

最终让 Agent 不再写死：

```python
SYSTEM_PROMPT = "你是一个专业助手..."
MODEL = "qwen..."
TEMPERATURE = 0.7
```

而变成：

```python
config = config_service.resolve(
    agent="test_case_agent",
    environment="prod",
    user_id=user_id
)
```

然后得到：

```json
{
  "prompt_version": "v12",
  "model": "qwen3.5-27b",
  "temperature": 0.2,
  "tools_version": "v5",
  "guardrail_version": "v3"
}
```

---

# 2. 为什么需要这个系统？

假设今天 Agent 使用：

```text
Prompt v10
Qwen3.5-27B
temperature=0.2
tools=v3
```

上线。

然后第二天：

```text
Prompt v11
```

上线。

结果：

```text
Agent Tool Call 错误率
5% → 30%
```

你需要知道：

```text
到底是什么变了？
```

如果没有 Config Versioning：

```text
昨天：
Prompt?
Model?
Tools?
Parameters?
```

全部说不清。

有版本管理：

```text
Execution #12345

agent:
test_agent

prompt:
test_agent:v11

model:
qwen3.5-27b:v2

tools:
tools:v5

guardrail:
guardrail:v3
```

那么可以精确复现。

---

# 3. 核心设计思想

整个系统围绕：

> **Immutable Version + Mutable Deployment**

展开。

也就是说：

### Version 不修改

例如：

```text
Prompt v1
Prompt v2
Prompt v3
```

一旦创建：

```text
v2
```

不能修改。

如果修改：

```text
v4
```

---

### Deployment 可以变化

例如：

```text
Production
   ↓
v3
```

然后：

```text
Production
   ↓
v4
```

甚至：

```text
Production

90% → v3
10% → v4
```

所以：

```text
Version
=
Immutable Artifact

Deployment
=
Mutable Routing
```

这是这个 Demo 最重要的架构概念。

---

# 4. 总体架构

```text
                         ┌──────────────┐
                         │   Web / CLI  │
                         └──────┬───────┘
                                │
                                ▼
                       ┌─────────────────┐
                       │  Config API     │
                       └────────┬────────┘
                                │
             ┌──────────────────┼──────────────────┐
             ▼                  ▼                  ▼
      Prompt Registry     Config Registry     Deployment
             │                  │                  │
             └──────────────────┼──────────────────┘
                                ▼
                         Version Resolver
                                │
                                ▼
                           A/B Router
                                │
                     ┌──────────┴──────────┐
                     ▼                     ▼
                 Version A             Version B
                     │                     │
                     └──────────┬──────────┘
                                ▼
                           Agent Runtime
                                │
                                ▼
                           LLM Gateway
                                │
                                ▼
                              LLM
```

旁边：

```text
PostgreSQL
Redis
Audit Log
Metrics
```

---

# 5. 技术栈

第一版推荐：

```text
Python 3.12
FastAPI
PostgreSQL
Redis
SQLAlchemy
Pydantic
```

前端可以暂时不要。

使用：

```text
Swagger
curl
pytest
```

完成 Demo。

后面再做：

```text
React / Vue
```

---

# 6. 项目目录

建议：

```text
prompt-config-registry/
│
├── README.md
├── pyproject.toml
├── docker-compose.yml
│
├── app/
│   │
│   ├── api/
│   │   ├── prompts.py
│   │   ├── configs.py
│   │   ├── deployments.py
│   │   └── resolve.py
│   │
│   ├── domain/
│   │   ├── prompt.py
│   │   ├── config.py
│   │   ├── deployment.py
│   │   └── experiment.py
│   │
│   ├── registry/
│   │   ├── prompt_registry.py
│   │   └── config_registry.py
│   │
│   ├── router/
│   │   ├── ab_router.py
│   │   ├── canary.py
│   │   └── hash_router.py
│   │
│   ├── resolver/
│   │   └── config_resolver.py
│   │
│   ├── deployment/
│   │   ├── publisher.py
│   │   └── rollback.py
│   │
│   ├── audit/
│   │   └── audit_service.py
│   │
│   └── storage/
│       ├── models.py
│       └── repository.py
│
├── migrations/
│
├── tests/
│   ├── test_prompt_version.py
│   ├── test_deployment.py
│   ├── test_ab_router.py
│   ├── test_canary.py
│   └── test_rollback.py
│
└── examples/
    └── agent.py
```

---

# 7. Prompt 数据模型

Prompt 不应该只是：

```text
prompt = "...";
version = 1
```

建议：

```python
@dataclass
class PromptVersion:

    id: str

    prompt_name: str

    version: int

    template: str

    variables: list[str]

    metadata: dict

    created_by: str

    created_at: datetime
```

例如：

```json
{
  "name": "test_case_agent",
  "version": 12,
  "template": "你是一名5G测试专家...",
  "variables": [
    "requirement",
    "context"
  ]
}
```

---

# 8. Prompt 必须 Immutable

例如：

```http
POST /v1/prompts/test_case_agent/versions
```

创建：

```text
v12
```

如果后来 Prompt 改成：

```text
你是一名专业的5G测试专家...
```

不能：

```http
PUT /v12
```

应该：

```text
v12
 ↓
create
 ↓
v13
```

原因很简单：

> **历史执行必须可复现。**

---

# 9. Config Version

Prompt 只是配置的一部分。

Agent 实际运行可能还有：

```text
Model
Temperature
Top P
Max Tokens
Tools
Tool Description
Guardrails
Retriever
Memory
```

因此可以定义：

```json
{
  "name": "test_case_agent",
  "version": 7,

  "model": {
    "provider": "qwen",
    "model": "qwen3.5-27b"
  },

  "parameters": {
    "temperature": 0.2,
    "top_p": 0.8,
    "max_tokens": 4096
  },

  "prompt": {
    "name": "test_case_agent",
    "version": 12
  },

  "tools": {
    "version": 5
  },

  "guardrails": {
    "version": 3
  }
}
```

---

# 10. 为什么 Config 还需要版本？

例如：

```text
Config v7
```

引用：

```text
Prompt v12
Model v3
Tools v5
Guardrails v3
```

下一次：

```text
Config v8
```

引用：

```text
Prompt v13
Model v3
Tools v5
Guardrails v3
```

这样你可以回答：

> Agent 行为发生变化，是因为 Prompt 从 v12 → v13。

---

# 11. Agent Config 应该成为一个 Snapshot

最终运行时拿到：

```json
{
  "agent": "test_case_agent",

  "config_version": 8,

  "prompt": {
    "name": "test_case_agent",
    "version": 13
  },

  "model": {
    "provider": "qwen",
    "model": "qwen3.5-27b"
  },

  "parameters": {
    "temperature": 0.2
  },

  "tools": [
    {
      "name": "search_kb",
      "version": 5
    }
  ]
}
```

这个对象就是：

> **Execution Configuration Snapshot**

---

# 12. Environment Binding

不同环境使用不同版本：

```text
dev
 ↓
v15

staging
 ↓
v14

prod
 ↓
v12
```

数据库：

```text
agent_deployments
```

例如：

```text
agent              environment       version
------------------------------------------------
test_case_agent    dev               v15
test_case_agent    staging           v14
test_case_agent    prod              v12
```

---

# 13. 为什么需要 Environment？

避免：

```text
开发者修改 Prompt
        ↓
直接影响生产
```

正确流程：

```text
dev
 ↓
test
 ↓
staging
 ↓
canary
 ↓
production
```

---

# 14. 发布流程

设计：

```text
DRAFT
  │
  ▼
VALIDATED
  │
  ▼
STAGED
  │
  ▼
CANARY
  │
  ▼
RELEASED
```

例如：

```text
Prompt v20
```

先：

```text
dev
```

然后：

```text
staging
```

然后：

```text
prod 5%
```

最后：

```text
prod 100%
```

---

# 15. A/B Router

最简单的：

```text
90%
v12

10%
v13
```

请求进入：

```text
A/B Router
```

然后：

```text
user_id
 ↓
hash
 ↓
bucket
```

例如：

```python
import hashlib


def bucket(user_id: str) -> int:

    h = hashlib.sha256(
        user_id.encode()
    ).hexdigest()

    return int(h[:8], 16) % 100
```

然后：

```python
if bucket(user_id) < 10:
    return "v13"

return "v12"
```

---

# 16. 为什么不能 random？

不要简单：

```python
random.random()
```

因为同一个用户可能：

```text
第一次 → v12
第二次 → v13
第三次 → v12
```

这样实验数据非常混乱。

Hash：

```text
user_id
 ↓
hash
 ↓
bucket
```

可以保证：

```text
同一个 user
→ 稳定命中同一个版本
```

这叫：

> **Sticky Assignment**

---

# 17. Canary

A/B 是：

```text
v12 vs v13
```

Canary 更偏向：

```text
旧版本
 ↓
5%
 ↓
10%
 ↓
30%
 ↓
50%
 ↓
100%
```

例如：

```text
v12 = 95%
v13 = 5%
```

监控：

```text
Error Rate
Latency
Token Cost
Tool Call Error
Task Success Rate
```

如果异常：

```text
Rollback
```

---

# 18. Rollback

假设：

```text
Production
v12
```

发布：

```text
v13
```

然后：

```text
Tool Error
5% → 20%
```

执行：

```http
POST /v1/deployments/rollback
```

最终：

```text
Production
 ↓
v12
```

注意：

> Rollback 不是删除 v13。

而是：

```text
Deployment
v13
 ↓
v12
```

版本本身仍然存在。

---

# 19. Deployment 数据模型

```python
@dataclass
class Deployment:

    id: str

    agent_name: str

    environment: str

    version: int

    traffic_percent: int

    status: str

    created_by: str

    created_at: datetime
```

---

# 20. 更进一步：Deployment Rule

实际不能只有：

```text
version = 13
```

应该支持：

```json
{
  "agent": "test_case_agent",

  "environment": "prod",

  "rules": [
    {
      "version": 12,
      "weight": 90
    },
    {
      "version": 13,
      "weight": 10
    }
  ]
}
```

以后甚至可以：

```json
{
  "rules": [
    {
      "condition": "tenant_id == 'internal'",
      "version": 13
    },
    {
      "condition": "user_region == 'SG'",
      "version": 13
    },
    {
      "default": true,
      "version": 12
    }
  ]
}
```

这就开始接近真正的：

> **Feature Flag / Configuration Routing System**

---

# 21. Config Resolver

Agent Runtime 不应该自己处理：

```text
dev/staging/prod
A/B
Canary
Rollback
```

而应该：

```python
config = resolver.resolve(
    agent="test_case_agent",
    environment="prod",
    user_id="user_123"
)
```

返回：

```python
AgentConfig
```

Resolver 内部：

```text
Agent
 │
 ▼
Environment Binding
 │
 ▼
Deployment Rules
 │
 ▼
A/B Router
 │
 ▼
Version
 │
 ▼
Config Snapshot
```

---

# 22. Resolver 是运行时核心

完整流程：

```text
resolve(agent, env, user)
              │
              ▼
        Get Deployment
              │
              ▼
        Check Rollout
              │
              ▼
          A/B Route
              │
              ▼
       Get Config Version
              │
              ▼
        Resolve Prompt
              │
              ▼
        Resolve Model
              │
              ▼
        Resolve Tools
              │
              ▼
      Build Config Snapshot
```

---

# 23. Cache

Config Resolver 不应该每一次都查 PostgreSQL。

可以：

```text
Redis
```

缓存：

```text
agent:test_case_agent:prod
```

例如：

```json
{
  "version": 13,
  "rules": [...]
}
```

但这里有一个非常重要的问题：

> **Config 更新后怎么让 Cache 失效？**

发布：

```text
v13
 ↓
Deployment Update
 ↓
DB
 ↓
Cache Invalidation
```

或者使用：

```text
Versioned Cache Key
```

例如：

```text
config:test_case_agent:prod:v13
```

---

# 24. Audit Log

每次：

```text
创建 Prompt
修改 Config
发布
灰度
回滚
```

都需要记录：

```json
{
  "operator": "alice",

  "action": "DEPLOY",

  "agent": "test_case_agent",

  "environment": "prod",

  "from_version": 12,

  "to_version": 13,

  "reason": "Improve tool selection",

  "timestamp": "..."
}
```

---

# 25. 为什么 Audit 非常重要？

以后线上出现：

```text
Agent突然开始疯狂调用Tool
```

可以查：

```text
22:01
Prompt v12

22:10
Prompt v13 deployed

22:15
Tool error ↑
```

于是：

```text
Root Cause
=
Prompt v13
```

这就是：

> **Change Attribution**

---

# 26. 数据库设计

### prompts

```sql
CREATE TABLE prompts (
    id UUID PRIMARY KEY,
    name VARCHAR(128) UNIQUE NOT NULL,
    created_at TIMESTAMP NOT NULL
);
```

### prompt_versions

```sql
CREATE TABLE prompt_versions (
    id UUID PRIMARY KEY,

    prompt_id UUID NOT NULL,

    version INT NOT NULL,

    template TEXT NOT NULL,

    variables JSONB,

    metadata JSONB,

    created_by VARCHAR(128),

    created_at TIMESTAMP NOT NULL,

    UNIQUE(prompt_id, version)
);
```

---

# 27. Agent Config

```sql
CREATE TABLE agent_configs (
    id UUID PRIMARY KEY,

    agent_name VARCHAR(128) NOT NULL,

    version INT NOT NULL,

    config JSONB NOT NULL,

    created_by VARCHAR(128),

    created_at TIMESTAMP NOT NULL,

    UNIQUE(agent_name, version)
);
```

---

# 28. Deployment

```sql
CREATE TABLE deployments (
    id UUID PRIMARY KEY,

    agent_name VARCHAR(128) NOT NULL,

    environment VARCHAR(32) NOT NULL,

    version INT NOT NULL,

    status VARCHAR(32) NOT NULL,

    traffic_percent INT NOT NULL,

    created_by VARCHAR(128),

    created_at TIMESTAMP NOT NULL
);
```

---

# 29. Audit

```sql
CREATE TABLE audit_logs (
    id BIGSERIAL PRIMARY KEY,

    actor VARCHAR(128),

    action VARCHAR(64),

    resource_type VARCHAR(64),

    resource_id VARCHAR(128),

    before JSONB,

    after JSONB,

    reason TEXT,

    created_at TIMESTAMP NOT NULL
);
```

---

# 30. API 设计

### 创建 Prompt

```http
POST /v1/prompts
```

### 创建 Prompt Version

```http
POST /v1/prompts/{name}/versions
```

### 查看版本

```http
GET /v1/prompts/{name}/versions
```

### 创建 Agent Config

```http
POST /v1/agents/{agent}/configs
```

### 发布

```http
POST /v1/deployments
```

### 灰度

```http
POST /v1/deployments/{id}/rollout
```

### 回滚

```http
POST /v1/deployments/{id}/rollback
```

### Runtime Resolve

```http
GET /v1/resolve
```

---

# 31. 最重要的 Resolve API

例如：

```http
GET /v1/resolve
    ?agent=test_case_agent
    &environment=prod
    &user_id=user_123
```

返回：

```json
{
  "agent": "test_case_agent",

  "config_version": 13,

  "prompt": {
    "name": "test_case_agent",
    "version": 20
  },

  "model": {
    "provider": "qwen",
    "name": "qwen3.5-27b"
  },

  "parameters": {
    "temperature": 0.2,
    "max_tokens": 4096
  },

  "tools": {
    "version": 8
  },

  "routing": {
    "experiment": "prompt_v20_test",
    "variant": "B"
  }
}
```

Agent Runtime 拿到之后直接执行。

---

# 32. 和 LLM Gateway 的关系

你前面做的：

```text
LLM Gateway
```

负责：

```text
Authentication
Rate Limit
Token Meter
Cache
Provider Routing
Streaming
```

而现在：

```text
Prompt Config Service
```

负责：

```text
Prompt
Model Config
Tool Version
Guardrail
Deployment
A/B
Canary
Rollback
```

因此：

```text
             Agent Runtime
                  │
          ┌───────┴────────┐
          ▼                ▼
 Config Resolver       LLM Gateway
          │                │
          │                ├── Rate Limit
          │                ├── Token
          │                ├── Cache
          │                └── Provider
          │
          ├── Prompt
          ├── Model
          ├── Tools
          └── Guardrails
```

两个系统职责非常清晰。

---

# 33. 和前面的 Semantic Cache 连接

这里还有一个很有意思的问题。

假设：

```text
Prompt v12
```

产生缓存：

```text
cache_key = semantic(user_question)
```

然后 Prompt 发布：

```text
v13
```

如果还使用旧缓存：

```text
User
 ↓
Prompt v13
 ↓
Semantic Cache
 ↓
命中 Prompt v12 产生的答案
```

可能出现：

> **新 Prompt + 旧答案**

所以 Semantic Cache 的 Key 最好包含：

```text
prompt_version
model_version
retrieval_version
tool_version
```

例如：

```text
semantic_cache_key =
    hash(
        normalized_prompt,
        prompt_version,
        model_version,
        config_version
    )
```

这样：

```text
Prompt v12
→ Cache A

Prompt v13
→ Cache B
```

这就是为什么你的几个 Demo 应该最终串起来。

---

# 34. 和 Token 计费连接

你的 Token Meter 可以记录：

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

这样以后你甚至可以分析：

```text
Prompt v12
平均 1800 tokens

Prompt v13
平均 2400 tokens
```

发现：

```text
Prompt v13
成本 +33%
```

这就是：

> **Configuration → Cost Attribution**

---

# 35. 和 Observability 连接

如果你后面使用：

```text
LangSmith
OpenTelemetry
Metrics
```

每一次 Trace 都应该记录：

```text
agent
config_version
prompt_version
model_version
tool_version
experiment
variant
```

例如：

```text
Trace #abc

agent:
test_case_agent

config:
v13

prompt:
v20

model:
qwen3.5-27b

experiment:
prompt_v20_test

variant:
B
```

这样才能分析：

```text
Variant A
Task Success = 87%

Variant B
Task Success = 91%
```

而不是只看到：

```text
LLM latency
```

---

# 36. 最重要的实验 Demo

建议这个项目一定做一个非常直观的实验：

```text
Prompt v1
```

Prompt：

```text
请简洁回答问题。
```

Prompt v2：

```text
请详细回答问题，并给出三个例子。
```

然后：

```text
Production
```

开始：

```text
100% → v1
```

之后：

```text
90% → v1
10% → v2
```

然后统计：

```text
              v1       v2

Success       82%      88%

Latency       1.2s     1.8s

Tokens        500      900

Cost          $0.01    $0.018
```

最终：

```text
v2
 ↓
100%
```

或者：

```text
v2
 ↓
Rollback
 ↓
v1
```

这会比单纯做 CRUD Demo 有价值很多。

---

# 37. 第二个实验：故障回滚

模拟：

```text
Prompt v10
```

正常：

```text
Tool Success = 95%
```

发布：

```text
Prompt v11
```

结果：

```text
Tool Success = 65%
```

系统：

```text
Metrics
 ↓
Anomaly Detection
 ↓
Rollback
 ↓
v10
```

最终：

```text
Production
     │
     ▼
    v11
     │
     │ Error ↑
     ▼
  Rollback
     │
     ▼
    v10
```

这就已经非常接近真正的 Agent Deployment Platform。

---

# 38. 最值得研究的一个问题：为什么版本要不可变？

面试很容易问：

> 为什么不直接修改 Prompt？

答案不是：

> 因为方便管理。

而是：

```text
LLM 是非确定系统
+
Prompt 是模型行为输入
+
Prompt 改变
=
系统行为可能改变
```

所以必须：

```text
Execution
 ↓
Snapshot
 ↓
Prompt v12
Model v3
Tools v5
Config v7
```

才能：

```text
Replay
Debug
Audit
Rollback
Experiment
```

---

# 39. 最终完整架构

把你现在前面的项目串起来：

```text
                         User
                           │
                           ▼
                    ┌─────────────┐
                    │ Agent API   │
                    └──────┬──────┘
                           │
                           ▼
                        Agent
                           │
                           ▼
                  Config Resolver
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
        Prompt         Model Config      Tools
        Registry         Registry        Registry
            │              │              │
            └──────────────┼──────────────┘
                           ▼
                      A/B Router
                           │
                    Canary / Rollout
                           │
                           ▼
                    Config Snapshot
                           │
                           ▼
                     LLM Gateway
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   RateLimiter        Semantic Cache      Token Meter
        │                  │                  │
        └──────────────────┼──────────────────┘
                           ▼
                          LLM
                           │
                           ▼
                       Tool Call
                           │
                           ▼
                       Sandbox
                           │
                           ▼
                         Result
```

旁边再配：

```text
OpenTelemetry
LangSmith
Audit Log
Metrics
PostgreSQL
Redis
```

---

# 40. 这个 Demo 的学习阶段

我建议你严格按照下面顺序写，而不是一开始就做“大而全”。

### Phase 1：Prompt Registry

实现：

```text
Prompt
Prompt Version
Immutable
CRUD
```

---

### Phase 2：Config Registry

加入：

```text
Model
Parameters
Tools
Guardrails
```

形成：

```text
AgentConfig
```

---

### Phase 3：Environment

实现：

```text
dev
staging
prod
```

以及：

```text
Binding
```

---

### Phase 4：Deployment

实现：

```text
Publish
Canary
A/B
Rollback
```

---

### Phase 5：Runtime Resolver

实现：

```python
resolve(
    agent,
    environment,
    user_id
)
```

让 Agent Runtime 真正消费配置。

---

### Phase 6：Observability

把：

```text
config_version
prompt_version
model_version
experiment_id
variant
```

写入：

```text
Trace
Metric
Token Usage
Audit
```

---

# 41. 最后你应该能回答这些面试题

这个项目重点准备：

1. **为什么 Prompt 需要版本管理？**
2. **为什么 Prompt Version 必须 Immutable？**
3. **Prompt Version 和 Config Version 有什么区别？**
4. **如何保证一次 Agent 执行可以 Replay？**
5. **A/B Router 为什么使用 Hash，而不是 Random？**
6. **如何保证同一个用户稳定命中同一个版本？**
7. **Canary 和 A/B Testing 有什么区别？**
8. **如何设计 Prompt Rollback？**
9. **Rollback 为什么不是删除新版本？**
10. **如何避免 Config Cache 导致配置不一致？**
11. **Prompt 更新后 Semantic Cache 怎么处理？**
12. **如何分析某个 Prompt Version 的 Token 成本？**
13. **如何知道 Agent 行为变化究竟是 Prompt、Model、RAG 还是 Tool 导致？**
14. **如何设计 Configuration Snapshot？**
15. **如何保证线上配置变更可审计？**

其中最核心的一条可以记住：

> **Prompt Registry 解决“存什么版本”；Deployment 解决“哪个环境用哪个版本”；Router 解决“这次请求用哪个版本”；Snapshot 解决“这次执行到底用了什么”；Audit/Observability 解决“为什么行为发生了变化”。**

如果你把 **01 LLM Gateway → 02 Token Metering → 03 Streaming → 04 Semantic Cache → 06 Job Queue → 07 Sandbox → 09 Prompt/Config Versioning** 按这个思路逐个手写出来，实际上已经不是在“学 Agent API”了，而是在逐步搭一个简化版的 **Agent Runtime / AI Infrastructure Platform**。
