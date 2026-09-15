# agent-memory

> **Agent 记忆系统（Agent Memory System）Demo —— 给无状态的 LLM 装上会「代谢」的记忆**
>
> 引擎 8 原语(Insert / SCD2 换代 / Delete / Consolidate / Retrieve / Touch / Decay / Wake)
> ＋ 四层记忆(Working / Short-Term / Episodic / Semantic·Profile) ＋ 后台蒸馏/衰减 Worker

对应设计说明书：`../第十一章：Agent 记忆系统设计说明书.md`

---

## 一句话定位

> LLM 本身无状态，上下文窗口又窄又贵。本 Demo 把 Agent 的记忆做成一个**分层 + 会代谢的有机体**：
> 确定性画像(Profile)走 Redis 点查静态热加载，经验(Episodic)走向量 Top-K 召回塞进工作记忆；
> 任务收官后离线把轨迹**蒸馏**成新记忆，长期没人用、置信度又低的边缘经验会被**艾宾浩斯衰减**成
> 沉睡态，一旦深潜检索命中再 **唤醒** 复活 —— 记忆库不是数据库的增删改，而是一套
> “感知提取 → 检索强化 → 冲突迭代 → 离线合并 → 代谢淘汰”的生命周期闭环。

```
                [ 8 原语 · 记忆的一生 ]
  Insert ──► Retrieve ──┬─► 命中并辅助成功 ──► Touch/Reinforce(强化突触)
                        ├─► 发现新事实冲突  ──► Update/Supersede(SCD2, 版本+1)
                        └─► 长期无人访问    ──► Decay/Forget(活力归零 → 沉睡)
                                                    │  碎片聚集 / 深潜命中
                        Consolidate(Merge→SOP) ◄────┼──► Wake/Re-activate(苏醒)
                        Delete(合规软删/物理硬删) ───┘

                [ 四层记忆 · 金字塔 ]
   用户请求 ──热注水──► ┌─────────────────────────┐
                        │ ① Working   (有预算的上下文帧) │
        Profile 点查(Redis) │ ② Short-Term(会话折叠+Checkpoint)│
        Episodic Top-K(向量) └────────────┬──────────────┘
        任务终态 ──► 离线蒸馏 ──► ③ Episodic / ④ Semantic·Profile
        定时 ──► Decay 代谢沉睡 ──► 深潜检索命中 ──► Wake 复活
```

---

## 特性一览

| 主题 | 落地 |
|---|---|
| **记忆引擎 8 原语** | `memory_engine/engine.py` 一个类集齐 Insert / SCD2 换代 / Delete / Consolidate / Retrieve(混合打分) / Touch / Decay / Wake；低层用 `dict` 模拟 PostgreSQL+pgvector |
| **四层记忆架构** | `tiers/` 四类各管一层：Working(预算) / Short-Term(折叠+快照) / Episodic(Top-K RAG) / Semantic+Profile(点查) |
| **跨层闭环编排** | `tiers/system.py`：热注水(Hydration) → 会话 → 任务终态蒸馏 → 碎片合并 SOP → 代谢/唤醒，一条龙 |
| **反思与蒸馏** | `worker/distill.py`：轨迹 → 「可行流程 / 避坑教训 / 反思规律」 |
| **遗忘与唤醒** | `worker/decay.py`：定时代谢调度器(`run()`/`run_once()`)，PROFILE 永不衰减 |
| **零依赖核心** | 引擎/分层/worker 只靠 stdlib(`math`/`zlib` 特征哈希伪嵌入)，不引 numpy/不联网 |
| **观测 API** | FastAPI `/v1/*`，把每个原语和每一层都暴露成可 curl 的端点 |

---

## 章节 ↔ 代码文件对照

| 说明书章节/要点 | 本 Demo 落地 |
|---|---|
| §核心操作的 Python 工程实现（MemoryRecord + 8 原语引擎） | `memory_engine/records.py`、`memory_engine/engine.py`（方法名与说明书一一对应） |
| §检索综合排名：相关性0.6+置信度0.2+艾宾浩斯抗衰0.2 | `engine.retrieve()` 的 `rank_score` 计算 |
| §Update/Supersede·SCD Type2 软失效 | `engine.update_supersede()` + `history()` 版本链 |
| §Consolidate 多条碎片蒸馏为高密度 SOP | `engine.consolidate()`（`predicate=consolidated_sop`，升阶 SEMANTIC） |
| §Touch 被引用后增强突触权重 | `engine.touch_reinforce()`（成功 +0.05；证伪 −0.25，<0.35 自动失活） |
| §Decay 基于艾宾浩斯与热度衰减评分 | `engine.run_decay_cycle()`（活力度 R = conf·(1+ln(1+访问))·e^(−λt)） |
| §Wake 深潜检索命中冷记忆后复活 | `engine.deep_retrieve()` + `engine.wake_reactivate()`（苏醒给中等置信 0.6） |
| §反思与蒸馏(从长轨迹提炼因果规律) | `worker/distill.py`（说明书代码块未展开的原语，此处补齐工程形态） |
| §经典四层记忆金字塔模型 | `tiers/{working,shortterm,episodic,semantic}.py` |
| §工作记忆：严格入模过滤 / 引用传递 | `tiers/working.py` 有预算的帧 + `assemble_context()` |
| §短期记忆：Checkpointer + 动态折叠 | `tiers/shortterm.py` `checkpoint()/restore()` + `fold()` |
| §情景记忆：离线沉淀 + Top-K RAG 动态检索 | `tiers/episodic.py` `save_fact()` + `recall()`(few-shot) |
| §语义与画像：确定性走点查、静态全量加载 | `tiers/semantic.py` `ProfileStore`/`SemanticMemory.point_query*` |
| §记忆层间「热注水→保鲜→蒸馏→代谢」闭环 | `tiers/system.py` `hydrate()/observe()/commit_session()/consolidate_sweep()` + `worker/decay.py` |

---

## 快速开始（零外部服务，纯 Python + 内存模拟）

```bash
cd agent-memory
pip install -e ".[test]"

# A) 三个 CLI 演示（推荐先跑）
python examples/demo_1_lifecycle.py    # 一条记忆的一生：8 原语走查(SCD2/证伪失活/合并SOP/衰减沉睡/唤醒)
python examples/demo_2_tiers.py        # 四层记忆 + 一次请求的热注水：模型"实际看到什么"
python examples/demo_3_workers.py      # 离线 Worker：蒸馏沉淀 + 时间快进后的遗忘代谢

# B) 观测 API（Swagger 即点即玩）
python examples/live_server.py         # http://localhost:8000/docs
```

跑测试：

```bash
python -m pytest tests/ -q    # 89 passed
```

---

## 用 API 亲手戳一戳 8 原语

> 提示：终端若为 GBK/乱码，请用 `-d @examples/xxx.json` 传载荷（本仓库已备好），或直接跑上面的 CLI demo。

```bash
# 检索：混合召回历史排障经验(few-shot)
curl -s -X POST http://localhost:8000/v1/recall -H "Content-Type: application/json" \
  -d @examples/curl_recall.json
# → hits[0].subject = "5G 基站闪断", 带 rank_score

# 任务收官：把轨迹蒸馏成记忆(可行流程 EPISODIC + 反思规律 SEMANTIC)
curl -s -X POST http://localhost:8000/v1/sessions/commit -H "Content-Type: application/json" \
  -d @examples/curl_commit.json

# 手动触发遗忘代谢(演示可把"世界"快进 120 天再跑, 不用真等)
curl -s -X POST http://localhost:8000/v1/workers/decay-run-once \
  -H "Content-Type: application/json" -d '{"simulate_idle_days":120}'

# 看四层记忆与统计
curl -s http://localhost:8000/v1/tiers/alice
curl -s http://localhost:8000/v1/system/stats
```

完整端点见下方 **API 一览**；`http://localhost:8000/docs` 有交互式文档。

---

## 一次把玩的直观闭环（demo_3 摘要）

```text
任务 A 成功收官 ──► 蒸馏出「可行流程」EPISODIC(conf 0.9) + 「反思规律」SEMANTIC(conf 0.6)
任务 B 踩坑失败 ──► 蒸馏出「避坑教训」EPISODIC(conf 0.7): 动作「盲目重启」会导致失败
3 条碎片积累     ──► consolidate_sweep ──► 合并升阶为 SEMANTIC SOP(碎片全部软下线)
让世界快进 90 天  ──► DecayWorker.run_once ──► 低活力 EPISODIC 被代谢成沉睡态
另一站点又报障    ──► 深潜检索命中沉睡记忆 ──► wake_reactivate 复活(conf 置 0.6 重新验证)
```

---

## API 一览

| 方法 | 路径 | 说明 | 对应 |
|---|---|---|---|
| POST | `/v1/memories` | 写入一条记忆(embedding 自动编码) | ① Insert |
| GET | `/v1/memories` | 列出(按 user/type/active 过滤) | 观测 |
| GET | `/v1/memories/{id}` | 查单条 | 观测 |
| POST | `/v1/memories/{id}/supersede` | SCD2 换代：旧版软失效, 派生 version+1 | ② Update/Supersede |
| DELETE | `/v1/memories/{id}?hard=` | 软删/硬删 | ③ Delete |
| POST | `/v1/memories/{id}/touch` | 召回后强化 / 证伪降权 | ⑥ Touch/Reinforce |
| POST | `/v1/memories/{id}/wake` | 唤醒沉睡冷记忆 | ⑧ Wake |
| POST | `/v1/recall` | 混合语义检索(活跃区) | ⑤ Retrieve |
| POST | `/v1/recall/sleeping` | 深潜检索：连沉睡记忆一起捞出(供 wake) | ⑧ 前置 |
| POST | `/v1/consolidate` | 碎片合并蒸馏为 SOP | ④ Consolidate |
| POST | `/v1/workers/decay-run-once` | 手动跑一轮遗忘代谢(可快进时间) | ⑦ Decay |
| POST | `/v1/sessions/observe` | 热注水：看一次请求装进工作记忆的内容 | 分层/热注水 |
| POST | `/v1/sessions/commit` | 任务终态：轨迹蒸馏沉淀 | 反思/蒸馏 |
| POST | `/v1/sessions/run-canned` | 一键回放三段式剧本 | 闭环演示 |
| GET | `/v1/tiers/{user_id}` | 四层记忆总览 | 分层 |
| GET | `/v1/system/stats` / `/v1/config` | 统计 / 当前参数 | 观测 |
| GET | `/healthz` | 存活检查 | 运维 |

---

## 目录结构

```
agent-memory/
├── memory_engine/
│   ├── records.py        # MemoryRecord：subject-predicate-object + 生命周期字段(照说明书)
│   ├── embedding.py      # 纯 stdlib：cosine_similarity + 字符 n-gram 特征哈希伪嵌入
│   ├── engine.py         # ★ MemoryEngine：8 原语(Insert/SCD2/Delete/Consolidate/Retrieve/Touch/Decay/Wake)
│   ├── config.py         # 全部旋钮(阈值/预算/半衰期/周期)，AGENTMEMORY_* 环境变量可覆写
│   ├── factory.py        # build_runtime() → Runtime：API / examples / tests 共用的装配把手
│   ├── tiers/
│   │   ├── working.py    # 第1层 工作记忆：有预算的上下文帧 + 严格入模过滤
│   │   ├── shortterm.py  # 第2层 短期记忆：会话轮次 + 折叠摘要 + Checkpointer 快照/恢复
│   │   ├── episodic.py   # 第3层 情景记忆：经验落库 + Top-K RAG(few-shot)
│   │   ├── semantic.py   # 第4层 语义+画像：ProfileStore 点查静态加载 / Semantic 规则
│   │   └── system.py     # 编排器：hydrate / observe / commit_session / consolidate_sweep
│   ├── worker/
│   │   ├── distill.py    # 反思与蒸馏：轨迹 → 可行流程 / 避坑教训 / 反思规律
│   │   └── decay.py      # DecayWorker：run()/run_once() 遗忘代谢调度(reaper 模式)
│   ├── api/              # 观测 API：schemas(pydantic) / routes(8 原语端点)
│   └── main.py           # FastAPI 入口(uvicorn memory_engine.main:app)，lifespan 挂后台 Worker
├── examples/
│   ├── demo_1_lifecycle.py / demo_2_tiers.py / demo_3_workers.py   # CLI 演示
│   ├── live_server.py    # 起观测服务
│   ├── curl_recall.json / curl_commit.json  # 规避 GBK 终端乱码的载荷样例
│   └── _lib.py
└── tests/                # 89 tests：records/embedding/engine/retrieve/decay/tiers/distill/api
```

---

## 你要真正搞懂的 8 个问题

1. **为什么画像(PROFILE)从不参与 Decay？**
   它是确定性硬偏好(语言/集群/编码习惯)，属于“永久真值”，衰减它等于把用户底细忘掉；
   而 EPISODIC 是自传经验、SEMANTIC 是归纳规则，需要新陈代谢保持信噪比。

2. **SCD2 换代为什么是“软失效 + 新版本”而不是 UPDATE 覆盖？**
   覆盖会丢失“它曾经信过什么”。软失效把旧版连同 `valid_to` 归档进版本链，
   审计回滚、纠偏对照(“为什么我现在不这么做了”)全靠历史链。

3. **Retrieve 的 rank_score 三项分别解决什么问题？**
   语义相似(0.6)保证“问什么答什么”；置信度(0.2)让“被反复验证的经验”排在“道听途说”前；
   艾宾浩斯抗衰(0.2)让“刚召回过/刚沉淀的”更靠前 —— 三者加权求和、互为补充：缺了置信度，
   一条“常被说但常错”的记忆会靠相似度挤到前面；缺了抗衰，一年前没人再碰的经验永远霸榜。

4. **“证伪”为什么比“遗忘”下手更狠？**
   Touch 失败分支一次 −0.25，是成功 +0.05 的 5 倍，并设 0.35 自动失活线：
   一条被现实打脸的记忆继续高置信输出比没有记忆更危险，宁可让它先沉睡。

5. **DecayWorker 怎么在测试/演示里不真等 30 天？**
   `run_once()` 把“跑一圈”与“循环多久跑一圈”解耦；配合 `engine.simulate_elapsed(days)`
   把整个世界快进，就可以确定性观察到一条记忆在 90 天后沉入沉睡态。

6. **醒来的记忆为什么只给 0.6 置信度？**
   沉睡过 = 长期未被验证。苏醒不等于直接回到巅峰，先给中等置信“重新上岗”，
   后续多次 Touch 成功再慢慢强化回去 —— 唤醒是给第二次机会，不是免检。

7. **工作记忆为什么永远保留最新一帧、只裁最旧的？**
   最新帧是“当前动作刚产生的工具返回/推理产物”，裁它等于把正在做的事扔掉；
   该被淘汰的是早先塞进来的旧历史 —— 所以淘汰策略是“预算超了就裁最旧的非保护帧”。

8. **轻量词袋嵌入为什么也能看？**
   `sim_threshold`(引擎默认 0.70 / 系统默认 0.30)是为“真实语义嵌入”与“词袋伪嵌入”
   分别校准的阈值；换用 OpenAI/Qwen 的 embedding 只需替换 `KeywordEmbedder`，
   引擎只认 `list[float]`，一行不改。

> **一句话面试答案**：Agent 记忆不是 CRUD。我按**四层金字塔**解耦：Working 管“模型此刻能看什么”
> (有预算、严格入模过滤)，Short-Term 管“这次会话聊到哪”(折叠+Checkpoint)，Episodic 管
> “我踩过什么坑”(向量 Top-K RAG)，Semantic+Profile 管“客观事实与用户硬偏好”(Redis 点查)；
> 四层之间靠热注水、任务终态蒸馏、碎片合并 SOP、以及基于艾宾浩斯曲线的 Decay/Wake 形成闭环，
> 让记忆库像大脑一样会遗忘、也会被重新想起。

---

## 与本书前面 Demo 的组合

```text
LLM 调用栈
  ├── 语义缓存(semantic-cache)        # 相同问题直接命中, 不重算
  ├── Prompt/配置版本(prompt-config-registry)
  ├── Guardrails 中间件(guardrails-middleware)   # 入参/出参把关
  ├── 流式基础设施 / 优雅中止(stream-infra / graceful-agent-cancel)
  └── 任务队列(async-agent-job-queue)  # 长任务先排队拿 task_id
        │
        ▼
   Agent 执行(本 Demo 的记忆引擎) —— 每次请求先 **热注水** 历史经验进工作记忆
   │       任务完成后异步入队「蒸馏 + 合并 + 代谢」Worker(可换成 async-agent-job-queue 的 worker)
   ▼
   Redis(Profile 点查) + 向量库(Episodic Top-K) + PostgreSQL(pgvector, 记忆主库)
```

本 Demo 用 `dict` 模拟 PG+pgvector、用特征哈希模拟真实 Embedding ——
把 `MemoryEngine.storage` 换成 pgvector 表、`KeywordEmbedder` 换成真实模型、`DecayWorker.run()`
换成 APScheduler/Celery beat，就是生产级形态。
