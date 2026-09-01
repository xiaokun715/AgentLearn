# semantic-cache — LLM 语义缓存系统

> **Semantic Cache 的本质：基于语义相似度的缓存。**

普通缓存只能命中「完全相同」的字符串；Semantic Cache 通过
**Embedding → 向量检索 → 相似度阈值**，让「字符串不同、语义高度相似」的问题
也命中缓存，从而省掉一次 LLM 调用。

```text
Query A  "什么是 TCP？"            Query B  "TCP 协议是什么？"
        │                                  │
        ▼                                  ▼
   Embedding                         Embedding
        │                                  │
        ▼                                  ▼
   Vector A                          Vector B
        │                                  │
        └────────────  Similarity = 0.92 ──┘
                        │
                        ▼
                    Cache HIT
                        │
                        ▼
                 直接返回历史答案（10ms，不调用 LLM）
```

---

## 1. 项目能力

- **Prompt Normalization** —— 解决空格 / 大小写 / 全半角 / 格式差异
- **Embedding** —— Mock（特征哈希，零依赖）/ Qwen（可切换，接口隔离）
- **Vector Similarity Search** —— Cosine Similarity + Top-K
- **Threshold Policy** —— 相似度阈值，控制错误命中
- **Cache Hit / Miss** —— Exact Cache（O(1)）+ Semantic Cache 两级缓存
- **TTL** —— 缓存自动过期
- **Invalidation** —— 按租户 / 模型 / 版本主动失效
- **Safety Validation** —— model / temperature / system prompt / 版本 / Agent Scope
- **Cache Metrics** —— Hit Rate / Token Saved / Cost Saved / Latency Saved
- **Agent 子任务缓存** —— agent_type / task_type 隔离的任务复用

---

## 2. 架构

```text
                         Client
                           │
                           ▼
                  ┌─────────────────┐
                  │   API Gateway   │
                  └────────┬────────┘
                           ▼
                  ┌─────────────────┐
                  │ PromptNormalizer│
                  └────────┬────────┘
                           ▼
                  ┌─────────────────┐
                  │ Semantic Cache  │
                  └────────┬────────┘
                    ┌──────┴──────┐
                   HIT           MISS
                    │             │
                    ▼             ▼
               Cached Answer   Embedding
                                  │
                                  ▼
                            Similarity Search (Top-K)
                                  │
                                  ▼
                            Threshold Policy
                                  │
                            ┌─────┴─────┐
                           HIT          MISS
                            │             │
                            ▼             ▼
                       Safety Validator  LLM
                            │             │
                            ▼             ▼
                         Answer       TokenMeter(Metrics)
                                          │
                                          ▼
                                        Cache Set
```

完整读路径（§32）：`Normalize → Exact Fingerprint → Exact HIT? → Embedding → Search Top-K → Threshold → Safety Validator → HIT`

> 为什么先 Exact 再 Semantic（§33）：Exact = Hash + O(1) 读取，几乎零成本；
> Semantic 需要 Embedding + 向量检索，本身有成本。顺序必须是 **Exact → Semantic → LLM**。

---

## 3. 目录结构

```text
semantic-cache/
├── semantic_cache/
│   ├── core/           # SemanticCache 主类、领域模型、策略
│   ├── normalize/      # Prompt Normalizer（§6~§9）
│   ├── embedding/      # Embedding 接口 + Mock + Qwen（§10~§11）
│   ├── search/         # Cosine 相似度 + pgvector SQL（§12~§14, §30~§31）
│   ├── safety/         # Safety Validator（§18~§20）
│   ├── storage/        # CacheStore 接口 + Memory + Postgres（§27~§29）
│   ├── invalidation/   # 失效管理（§24~§25）
│   ├── metrics/        # Cache Metrics + Prometheus 渲染（§41~§46）
│   ├── api/            # HTTP 路由（§48）
│   ├── llm/            # Mock LLM（§51）
│   ├── factory.py      # 依赖装配
│   └── main.py         # FastAPI 入口
├── migrations/         # PostgreSQL + pgvector 建表 SQL
├── tests/              # 单元 + 集成测试（67 个用例）
├── examples/           # basic / agent_cache
└── experiments/        # 四个实验（§53）
```

---

## 4. 快速开始

```bash
# 安装（含测试依赖）
pip install -e ".[test]"

# 跑测试
pytest

# 运行基础 Demo：直观看到 1s(MISS) -> 10ms(HIT)
python examples/basic.py

# 运行 Agent 子任务缓存 Demo
python examples/agent_cache.py

# 启动 HTTP 服务
uvicorn semantic_cache.main:app --reload --port 8000
```

HTTP 服务启动后：

```bash
# 第一次：MISS（调用 Mock LLM）
curl -s http://localhost:8000/v1/chat -H "Content-Type: application/json" \
  -d '{"user_id":"user-001","model":"qwen","tenant_id":"tenant-A",
       "messages":[{"role":"user","content":"什么是TCP协议？"}]}'

# 第二次：同义表达，Semantic HIT
curl -s http://localhost:8000/v1/chat -H "Content-Type: application/json" \
  -d '{"user_id":"user-001","model":"qwen","tenant_id":"tenant-A",
       "messages":[{"role":"user","content":"TCP协议是什么？"}]}'

# 指标
curl -s http://localhost:8000/metrics
```

---

## 5. API（§48）

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/v1/chat` | 聊天入口。返回体中带 `cache: {hit, source, similarity, confidence, latency_ms}` |
| GET | `/metrics` | Prometheus 格式指标 |
| GET | `/health` | 健康检查 + 当前缓存大小 / 命中率 |
| POST | `/admin/invalidate` | 主动失效，body 可传 `tenant_id` / `model` / `knowledge_version` / `cache_id` 等 |

`/v1/chat` 响应示例（命中时）：

```json
{
  "choices": [{"message": {"role": "assistant", "content": "..."}}],
  "usage": {"prompt_tokens": 120, "completion_tokens": 80},
  "cache": {"hit": true, "source": "semantic", "similarity": 0.92, "confidence": 0.90, "latency_ms": 8.1}
}
```

---

## 6. 核心设计要点

### 6.1 Prompt Normalization（§6 ~ §9）
- 字符串层面归一化（NFKC 全半角、折叠空白、统一大小写），解决**形式差异**；
- 语义差异交给 Embedding 解决，二者不是一回事（§7）；
- **Cache Key 必须包含 system prompt / model / temperature / 版本**，否则不同 System Prompt 会错误命中（§8）；
- 精确指纹 = canonical request 的 SHA256（§9）。

### 6.2 Embedding（§10 ~ §11）
- 抽象 `EmbeddingGenerator` 接口，Demo 用 `MockEmbeddingGenerator`（字符 n-gram 特征哈希 + L2 归一化，零依赖、确定性）；
- 生产替换 `QwenEmbeddingGenerator`（DashScope text-embedding-v4）即可，Cache 层零改动；
- **Mock 相似度分布**：同义 ~0.90+，相关但不同 ~0.78 以下，完全不同 <0.50。

### 6.3 Similarity Search（§12 ~ §14）
- Cosine Similarity：`cos(A,B) = A·B / (|A||B|)`，越接近 1 越相似；
- Top-K 候选（默认 5），而不是只找最近的一个。

### 6.4 Threshold Policy（§15 ~ §16）
- 默认阈值 `0.83`（为 Mock 向量标定）；
- **不要简单认为 0.9 永远是 HIT**：阈值必须通过正负样本对数据集评估，见实验二。

### 6.5 Safety Validator（§18 ~ §20）
语义命中后仍要二次校验，否则就是 **错误命中（False Positive）** —— Semantic Cache 最大的风险：

- model / temperature / namespace
- **system prompt 指纹**（不同 System Prompt 不能共享缓存，§8）
- **knowledge_version**（知识库 v42 ≠ v43 直接 MISS，§25）
- **agent_type / task_type / context_version**（Agent 之间不能错误复用，§38）

### 6.6 租户隔离（§20）
**必须实现的安全边界**：缓存条目带 `tenant_id`，检索时 `WHERE tenant_id = current_tenant`，
否则 Tenant A 的内部数据会被 Tenant B 语义命中，造成跨租户数据泄露。

### 6.7 TTL 与 Invalidation（§22 ~ §25）
- TTL 默认 1 小时，实时性问题不缓存；
- 主动失效：按租户 / 模型 / 版本批量删除；
- **Version-based Invalidation**：知识库更新后只失效旧版本条目，不用遍历删除所有缓存。

### 6.8 Cacheability Policy（§35 ~ §36）
以下请求**不写入缓存**：temperature 过高、携带 tools、实时性问题、破坏性指令（blocklist）。

### 6.9 Agent Cache（§37 ~ §39）
Agent 产生大量重复/相似子任务，非常适合语义缓存；
但必须区分 **Semantic Similarity** 与 **Business Identity** ——
「分析 TCP timeout 日志A」与「分析 TCP timeout 日志B」文本相似、答案却不同。

### 6.10 Metrics（§41 ~ §46）
不能只看 Hit Rate（90% 命中但 10% 错误命中更危险）。至少同时观察：
`Hit Rate / False Hit Rate / Token Saved / Cost Saved / Latency Saved`。

---

## 7. PostgreSQL + pgvector 升级路径

Demo 默认 `MemoryStore`；生产推荐 `PostgreSQL + pgvector`（§28 ~ §31）：

```bash
docker compose up -d          # 首次启动自动应用 migrations/001_create_cache.sql
pip install semantic-cache[postgres]
export DATABASE_URL="postgresql://semantic:semantic@localhost:5432/semantic_cache"
export CACHE_STORE=postgres
```

关键 SQL 要点（§31）：**先过滤元数据再排序**，保证租户隔离 / TTL / 相似度同时成立：

```sql
SELECT ..., 1 - (embedding <=> $5::vector) AS similarity
FROM semantic_cache
WHERE namespace=$1 AND tenant_id=$2 AND model=$3
  AND expires_at > NOW()
ORDER BY embedding <=> $5::vector
LIMIT $4;
```

---

## 8. 测试矩阵（§52）

| Case          | Query                      | Expected |
| ------------- | -------------------------- | -------- |
| Exact         | 完全相同                     | HIT      |
| Whitespace    | 空格不同                     | HIT      |
| Synonym       | 同义表达（什么是TCP / TCP是什么）| HIT      |
| Related       | 相关但不同（TCP / UDP）      | MISS     |
| Different     | 完全不同（TCP / 天气）       | MISS     |
| Expired       | TTL 超时                    | MISS     |
| Tenant        | 不同 Tenant                 | MISS     |
| Model         | 不同 Model                  | MISS     |
| System Prompt | 不同 System                 | MISS     |
| Version       | KB Version 不同             | MISS     |
| Tool          | Tool 参数不同                | MISS     |
| Realtime      | 实时问题                     | MISS     |

---

## 9. 四个实验（§53）

```bash
python experiments/experiment_1_exact_vs_semantic.py   # Exact 命中不了，Semantic 能
python experiments/experiment_2_threshold.py            # 阈值 vs Precision/Recall/False Hit
python experiments/experiment_3_ttl.py                  # TTL 到期自动 MISS
python experiments/experiment_4_agent_cache.py          # Agent 子任务命中率 / Token / Cost / Latency
```

---

## 10. 面试问题清单（§56）

**基础**：什么是 Semantic Cache？与 Redis Cache 的区别？为什么 Embedding 可以用于缓存？

**算法**：Cosine Similarity 怎么算？Threshold 怎么确定？为什么不能简单设 0.9？

**工程**：为什么需要 Prompt Normalization？为什么 Exact + Semantic 两级缓存？Cache Entry 有哪些字段？

**一致性**：TTL 怎么设计？知识库更新后怎么失效？为什么 Version-based Invalidation 更好？

**安全**：如何避免跨 Tenant 数据泄露？不同 System Prompt 能否共享？不同 Agent 能否共享？

**成本**：怎么计算节省的 Token / 钱 / 延迟？

**可靠性**：Semantic Cache 最大的风险是什么？→ **False Positive / 错误命中**。

---

## 11. 与前面三个项目的关系（§54 ~ §55）

| 项目 | 回答的问题 |
| ---- | ---------- |
| OpenGateway | 请求应该发给谁？ |
| TokenMeter | 这个请求用了多少资源？ |
| StreamInfra | 结果如何实时、可靠地交付？ |
| **SemanticCache** | **这个请求是否可以根本不用调用 LLM？** |

整合架构：

```text
Client → OpenGateway(Auth) → RateLimiter → SemanticCache
   ├── HIT  → 直接返回
   └── MISS → ModelRouter → LLM Provider → TokenMeter / StreamInfra → Cache Set
```

这四个项目合起来，就从 **LLM Application** 进入了 **LLM Infrastructure**。
