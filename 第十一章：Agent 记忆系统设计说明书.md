在工业级生产环境中，Agent 记忆的生命周期远不止基础的 CRUD（增删改查）。一套完整的记忆引擎通常包含 **8 种核心原语操作**：

1. **添加（Insert / Form）**：初始抽取与格式化写入。
2. **更新（Update / Supersede）**：新旧版本迭代与软失效（SCD Type 2）。
3. **删除（Delete / Evict）**：主动遗忘或合规物理抹除。
4. **合并（Consolidate / Merge）**：多条细碎事实聚类并提炼为高密度 SOP。
5. **检索（Retrieve / Recall）**：精确元数据过滤与向量语义混合搜索。
6. **反思与蒸馏（Reflect / Distill）**：从长任务轨迹中提炼因果规律。
7. **遗忘与衰减（Decay / Forget）**：基于艾宾浩斯时间与热度衰减评分。
8. **反哺与强化（Touch / Reinforce）**：被引用后增强突触权重（置信度提升、时间戳刷新）。

---

### 核心操作的 Python 工程实现

以下代码基于 `dataclass` 与向量数学模拟实现这 8 种核心原子操作：

```python
import math
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

@dataclass
class MemoryRecord:
    memory_id: str
    user_id: str
    subject: str
    predicate: str
    object_value: str
    memory_type: str                  # PROFILE, EPISODIC, SEMANTIC
    confidence: float = 1.0           # 0.0 ~ 1.0
    embedding: List[float] = field(default_factory=list)
    version: int = 1
    is_active: bool = True
    access_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_to: Optional[datetime] = None

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    norm_a = math.sqrt(sum(a * a for a in v1))
    norm_b = math.sqrt(sum(b * b for b in v2))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class ProductionAgentMemoryEngine:
    def __init__(self):
        # 模拟底层存储 (PostgreSQL + pgvector)
        self.storage: Dict[str, MemoryRecord] = {}

    # -------------------------------------------------------------
    # 1. 添加 (Insert / Form)
    # -------------------------------------------------------------
    def insert(self, user_id: str, subject: str, predicate: str, 
               object_value: str, memory_type: str, embedding: List[float]) -> MemoryRecord:
        """初始写入一条新事实"""
        mem_id = f"mem_{uuid.uuid4().hex[:8]}"
        record = MemoryRecord(
            memory_id=mem_id,
            user_id=user_id,
            subject=subject,
            predicate=predicate,
            object_value=object_value,
            memory_type=memory_type,
            embedding=embedding
        )
        self.storage[mem_id] = record
        return record

    # -------------------------------------------------------------
    # 2. 更新与版本迭代 (Update / Supersede - SCD Type 2)
    # -------------------------------------------------------------
    def update_supersede(self, old_mem_id: str, new_value: str, 
                         new_embedding: List[float], confidence: float = 1.0) -> MemoryRecord:
        """软失效旧版本，自增版本号插入新记录，避免物理覆盖"""
        old_record = self.storage[old_mem_id]
        now = datetime.now(timezone.utc)
        
        # 旧版本归档关闭
        old_record.is_active = False
        old_record.valid_to = now
        
        # 写入新版本
        new_id = f"mem_{uuid.uuid4().hex[:8]}"
        new_record = MemoryRecord(
            memory_id=new_id,
            user_id=old_record.user_id,
            subject=old_record.subject,
            predicate=old_record.predicate,
            object_value=new_value,
            memory_type=old_record.memory_type,
            confidence=confidence,
            embedding=new_embedding,
            version=old_record.version + 1,
            created_at=now,
            last_accessed_at=now
        )
        self.storage[new_id] = new_record
        return new_record

    # -------------------------------------------------------------
    # 3. 删除 / 淘汰 (Delete / Evict)
    # -------------------------------------------------------------
    def delete(self, mem_id: str, hard_delete: bool = False):
        """支持软删除(合规/安全)与物理删除"""
        if mem_id not in self.storage:
            return
        if hard_delete:
            del self.storage[mem_id]
        else:
            self.storage[mem_id].is_active = False
            self.storage[mem_id].valid_to = datetime.now(timezone.utc)

    # -------------------------------------------------------------
    # 4. 合并与压缩 (Consolidate / Merge)
    # -------------------------------------------------------------
    def consolidate(self, target_mem_ids: List[str], generalized_value: str, 
                    new_embedding: List[float]) -> MemoryRecord:
        """将多条细碎的情景经验合并蒸馏为单条高阶抽象规则"""
        records = [self.storage[mid] for mid in target_mem_ids if mid in self.storage]
        if not records:
            raise ValueError("没有可合并的有效记录")
            
        base = records[0]
        now = datetime.now(timezone.utc)
        
        # 批量软下线碎片记忆
        for r in records:
            r.is_active = False
            r.valid_to = now
            
        # 写入合并后的抽象规则 (升阶为 SEMANTIC 语义记忆)
        new_id = f"mem_{uuid.uuid4().hex[:8]}"
        consolidated = MemoryRecord(
            memory_id=new_id,
            user_id=base.user_id,
            subject=base.subject,
            predicate="consolidated_sop",
            object_value=generalized_value,
            memory_type="SEMANTIC",
            confidence=min(1.0, sum(r.confidence for r in records) / len(records) + 0.1),
            embedding=new_embedding,
            created_at=now,
            last_accessed_at=now
        )
        self.storage[new_id] = consolidated
        return consolidated

    # -------------------------------------------------------------
    # 5. 检索与召回 (Retrieve / Recall)
    # -------------------------------------------------------------
    def retrieve(self, user_id: str, query_embedding: List[float], 
                 subject_filter: Optional[str] = None, top_k: int = 3, 
                 sim_threshold: float = 0.70) -> List[Tuple[MemoryRecord, float]]:
        """结合余弦相似度、置信度与时间衰减的复合混合检索"""
        scored_candidates = []
        now = datetime.now(timezone.utc)

        for record in self.storage.values():
            # 状态隔离门禁
            if not record.is_active or record.user_id != user_id:
                continue
            if subject_filter and record.subject != subject_filter:
                continue

            sim = cosine_similarity(query_embedding, record.embedding)
            if sim < sim_threshold:
                continue

            # 综合计算记忆排名得分 (相关性 0.6 + 置信度 0.2 + 艾宾浩斯抗衰度 0.2)
            days_passed = (now - record.last_accessed_at).total_seconds() / 86400.0
            time_decay = math.exp(-0.02 * days_passed)
            rank_score = (sim * 0.6) + (record.confidence * 0.2) + (time_decay * 0.2)
            
            scored_candidates.append((record, rank_score))

        # 按最终排分倒序截取
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        return scored_candidates[:top_k]

    # -------------------------------------------------------------
    # 6. 反哺与突触强化 (Touch / Reinforce)
    # -------------------------------------------------------------
    def touch_reinforce(self, memory_id: str, task_succeeded: bool = True):
        """当记忆被召回并成功指导任务时，强化其突触权重；若被证伪则弱化"""
        if memory_id not in self.storage:
            return
        record = self.storage[memory_id]
        record.access_count += 1
        record.last_accessed_at = datetime.now(timezone.utc)

        if task_succeeded:
            # 强化置信度与存活力
            record.confidence = min(1.0, record.confidence + 0.05)
        else:
            # 削弱置信度，降至阈值下自动失活
            record.confidence = max(0.0, record.confidence - 0.25)
            if record.confidence < 0.35:
                self.delete(memory_id, hard_delete=False)

    # -------------------------------------------------------------
    # 7. 遗忘与新陈代谢 (Decay / Forget)
    # -------------------------------------------------------------
    def run_decay_cycle(self, half_life_days: float = 30.0, retention_floor: float = 0.2):
        """定时任务：模拟大脑睡眠时的遗忘代谢机制，自动软失效边缘低活记忆"""
        now = datetime.now(timezone.utc)
        for record in self.storage.values():
            if not record.is_active or record.memory_type == "PROFILE":
                # 用户画像属于长久静态事实，不参与自动衰减淘汰
                continue

            days_idle = (now - record.last_accessed_at).total_seconds() / 86400.0
            # 活力度评分 = confidence * (1 + ln(1 + 访问次数)) * e^(-λ * t)
            lambda_param = math.log(2) / half_life_days
            vitality = record.confidence * (1.0 + math.log1p(record.access_count)) * math.exp(-lambda_param * days_idle)

            if vitality < retention_floor:
                # 活力耗尽，进入沉睡失活状态
                record.is_active = False
                record.valid_to = now

    # -------------------------------------------------------------
    # 8. 唤醒与冷激活 (Wake / Re-activate)
    # -------------------------------------------------------------
    def wake_reactivate(self, memory_id: str):
        """当外部深度检索命中已失活的冷记忆时，将其重新唤醒回活跃区"""
        if memory_id in self.storage:
            record = self.storage[memory_id]
            record.is_active = True
            record.valid_to = None
            record.confidence = 0.6  # 初始恢复给中等置信度
            record.last_accessed_at = datetime.now(timezone.utc)

```

---

### 全操作协同运转图解

```
                   [ 1. Insert (初始落盘) ]
                              │
                              ▼
            ┌─────── [ 5. Retrieve (混合检索) ] ◄────────┐
            │                 │                          │
            │ 命中并辅助成功   │ 发现新事实产生冲突        │ 长期无人访问
            ▼                 ▼                          │
[ 6. Touch/Reinforce ]  [ 2. Update/Supersede ]   [ 7. Decay/Forget ]
(增加计数, 强化置信度)   (旧版软失效, 派生版本+1)   (活力归零, 软失效沉睡)
                              │                          │
                              │ 碎片聚集                 │ 命中深潜检索
                              ▼                          ▼
                        [ 4. Consolidate ]        [ 8. Wake/Reactivate ]
                        (多条蒸馏合并为 SOP)       (重新激活赋予新生命周期)
                              │
                              ▼
                        [ 3. Delete ]
                        (合规要求强制抹除)

```

通过这 8 个 API，Agent 的记忆库跳出了单纯的数据库增删改，形成了“感知提取 $\to$ 检索强化 $\to$ 冲突迭代 $\to$ 离线合并 $\to$ 代谢淘汰”的闭环有机体。



**分层记忆机制（Tiered Memory Architecture）**是生产级智能体（Agent）系统的核心基础设施。大模型本身是无状态的，且受限于物理上下文窗口（Context Window）的容量、注意力稀释（Lost-in-the-Middle）以及 Token 成本。分层记忆机制借鉴了**现代计算机的多级存储体系（CPU寄存器 $\to$ 缓存 $\to$ 内存 $\to$ 磁盘）**与**认知神经科学的记忆模型**，将信息按**时效性、读写频次、确定性与抽象度**进行解耦管理。

---

### 一、 经典四层记忆金字塔模型

从快到慢、从瞬态到持久，工业界标准的 Agent 记忆分层如下：

```
                    ┌─────────────────────────┐
                    │  1. 工作记忆 (Working)  │ ──> CPU 寄存器 / Context Window
                    ├─────────────────────────┤
                    │  2. 短期记忆 (Short-Term)│ ──> 内存 (RAM) / Redis Session
                    ├─────────────────────────┤
                    │  3. 情景记忆 (Episodic) │ ──> SSD 硬盘 / 向量库 (按时序与经验归档)
                    ├─────────────────────────┤
                    │  4. 语义/程序记忆 (Long)│ ──> 数据中台 / 知识图谱 / 技能库 / Profile
                    └─────────────────────────┘

```

| 记忆层级 | 计算机映射 | 存储介质 | 读写延迟 | 生命周期 | 核心内容与职责 |
| --- | --- | --- | --- | --- | --- |
| **工作记忆 (Working Memory)** | 寄存器 / L1 Cache | 模型当前 Context Window | 0 (即时) | 单步推理周期 | 当前步的 Thought、正在使用的 Tool Result、System Prompt 约束 |
| **短期记忆 (Short-Term Memory)** | 物理内存 (RAM) | Redis / PostgreSQL Checkpointer | 1 ~ 5 ms | 当前 Session / 任务会话 | 连续多轮对话历史、任务中间规划状态（Scratchpad）、断点快照 |
| **情景记忆 (Episodic Memory)** | SSD 硬盘 (时序归档) | 向量数据库 (Qdrant / Milvus / pgvector) | 20 ~ 50 ms | 长期 / 跨会话 | “上次遇到基带告警 X 时是如何排查的”历史任务执行轨迹与踩坑教训 |
| **语义与画像记忆 (Semantic & Profile)** | 固件 / 全局数据库 | 关系表 (PostgreSQL) / 键值存储 (Redis) | 1 ~ 10 ms | 永久 / 全生命周期 | 客观领域知识、3GPP 规范、用户硬性偏好（环境配置、编程语言） |

---

### 二、 各层核心职责与运转方式

#### 1. 工作记忆（Working Memory / Sensory Memory）

* **本质**：就是**大模型在执行当前推理步时，注意力机制实际计算的 Token 窗口**。
* **特点**：极其宝贵且排他，容量狭窄。
* **管理原则**：**严格做入模过滤**。未剪枝的几十 MB 原生日志、已完成历史步骤的冗余工具返回，绝对禁止直接塞入工作记忆，必须通过引用传递（Pass-by-Reference）或瞬态修剪转化为轻量摘要。

#### 2. 短期与会话记忆（Short-Term Memory）

* **本质**：维持**单次连续会话或单个长任务运行周期**的状态机容器。
* **运转机制**：
* **状态快照（Checkpointer）**：每轮交互实时更新，保证服务崩溃重启时可恢复断点；
* **动态折叠（In-flight Pruning）**：当对话超出一定轮数时，利用轻量模型将 $N-2$ 步之前的历史折叠为简短的事实摘要，释放上下文空间；
* **任务结束即消亡**：会话彻底结束后，短期记忆退火，转入冷存储或等待后台抽取。



#### 3. 情景记忆（Episodic Memory）

* **本质**：Agent 的**自传式经验归档（“我过去经历过什么，踩过什么坑，最后怎么解决的”）**。
* **运转机制**：
* **离线沉淀**：任务完成后，由后台 Worker 异步提取成功/失败的复盘经验；
* **动态检索（Top-K RAG）**：遇到类似新任务时，基于当前 Query 计算语义向量，从向量库中召回最相似的 2~3 个历史案例作为 Few-Shot 注入工作记忆；
* **时效衰减**：引入艾宾浩斯遗忘曲线，长时间未被召回的边缘经验逐步软失效。



#### 4. 语义记忆与用户画像（Semantic Memory & Profile）

* **本质**：Agent 对**客观世界事实、行业领域规律与用户特质的确定性认知**。
* **运转机制**：
* **用户画像（User Profile）**：存储在 Redis Hash 中，用户发起请求时**直接全量静态加载**，不走向量搜索，保证 100% 命中且零检索耗时；
* **程序技能（Procedural Skills）**：固化为标准代码工具（Tools）或 SOP 工作流规则库，指导 Agent 标准化作业。



---

### 三、 记忆层级间的“流转与代谢”闭环

记忆分层不是静态隔离的，数据在各层之间经历“过滤 $\to$ 固化 $\to$ 衰减 $\to$ 唤醒”的生命周期闭环：

```
                             [ 用户提问 / 物理环境输入 ]
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  ▼ (静态热加载, <1ms)                              ▼ (动态语义检索, ~30ms)
        【用户画像 (Profile / Redis)】                  【情景经验 (Episodic / 向量库)】
        • 默认集群偏好、语言风格                         • 召回最匹配的历史避坑经验
                  │                                               │
                  └───────────────────────┬───────────────────────┘
                                          │
                                          ▼
                             ┌─────────────────────────┐
                             │ 工作记忆 (Context 组装) │
                             └────────────┬────────────┘
                                          │
                                          ▼ (单步推理与 Tool 调度)
                             ┌─────────────────────────┐
                             │ 短期状态 (Checkpoint)   │
                             └────────────┬────────────┘
                                          │
                                          ▼ (任务终态 / 会话超时 Idle)
                          [ 后台异步记忆固化 (Consolidation) ]
                                          │
            ┌─────────────────────────────┼─────────────────────────────┐
            ▼ (概念抽象与泛化)             ▼ (经验沉淀与去重)             ▼ (画像变更)
   ┌─────────────────┐           ┌─────────────────┐           ┌─────────────────┐
   │ 语义记忆/规则库 │           │ 情景记忆 (向量) │           │ 用户档案 (Redis)│
   └─────────────────┘           └─────────────────┘           └─────────────────┘

```

1. **热注水（Hydration）**：请求进入时，从 Redis 提取静态画像，从向量库召回动态经验，合并拼装进工作记忆；
2. **瞬态保鲜（Checkpointer Sync）**：执行过程中，仅向短期缓存写入状态，保障会话级连贯与容灾；
3. **经验蒸馏（Consolidation）**：任务达成终态后，异步 Worker 离线介入，剔除中间试错，提炼高价值因果事实；
4. **新陈代谢（Decay & Forgetting）**：通过定时任务计算活力分 $R = \text{confidence} \times (1 + \ln(1 + \text{access\_count})) \times e^{-\lambda \Delta t}$，淘汰长期无用的冗余记忆，保持系统高信噪比。

---

### 四、 为什么必须分层？（工程收益）

* **规避上下文爆炸与高昂 Token 成本**：将 95% 的历史信息剥离到外存，Context 始终维持在最精简的几千 Token 内；
* **消除检索幻觉与记忆分裂**：确定性配置（画像）走点查，经验走向量，杜绝了向量近似计算带来的漏召回与误召回；
* **保障极致交互体验**：耗时极长的提取、Embedding、冲突消解全部下沉到异步离线层，主对话链路实现零延迟毫秒级流式响应。


在工业级生产环境中，Agent 记忆的生命周期远不止基础的 CRUD（增删改查）。一套完整的记忆引擎通常包含 **8 种核心原语操作**：

1. **添加（Insert / Form）**：初始抽取与格式化写入。
2. **更新（Update / Supersede）**：新旧版本迭代与软失效（SCD Type 2）。
3. **删除（Delete / Evict）**：主动遗忘或合规物理抹除。
4. **合并（Consolidate / Merge）**：多条细碎事实聚类并提炼为高密度 SOP。
5. **检索（Retrieve / Recall）**：精确元数据过滤与向量语义混合搜索。
6. **反思与蒸馏（Reflect / Distill）**：从长任务轨迹中提炼因果规律。
7. **遗忘与衰减（Decay / Forget）**：基于艾宾浩斯时间与热度衰减评分。
8. **反哺与强化（Touch / Reinforce）**：被引用后增强突触权重（置信度提升、时间戳刷新）。

---

### 核心操作的 Python 工程实现

以下代码基于 `dataclass` 与向量数学模拟实现这 8 种核心原子操作：

```python
import math
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

@dataclass
class MemoryRecord:
    memory_id: str
    user_id: str
    subject: str
    predicate: str
    object_value: str
    memory_type: str                  # PROFILE, EPISODIC, SEMANTIC
    confidence: float = 1.0           # 0.0 ~ 1.0
    embedding: List[float] = field(default_factory=list)
    version: int = 1
    is_active: bool = True
    access_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_to: Optional[datetime] = None

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    norm_a = math.sqrt(sum(a * a for a in v1))
    norm_b = math.sqrt(sum(b * b for b in v2))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class ProductionAgentMemoryEngine:
    def __init__(self):
        # 模拟底层存储 (PostgreSQL + pgvector)
        self.storage: Dict[str, MemoryRecord] = {}

    # -------------------------------------------------------------
    # 1. 添加 (Insert / Form)
    # -------------------------------------------------------------
    def insert(self, user_id: str, subject: str, predicate: str, 
               object_value: str, memory_type: str, embedding: List[float]) -> MemoryRecord:
        """初始写入一条新事实"""
        mem_id = f"mem_{uuid.uuid4().hex[:8]}"
        record = MemoryRecord(
            memory_id=mem_id,
            user_id=user_id,
            subject=subject,
            predicate=predicate,
            object_value=object_value,
            memory_type=memory_type,
            embedding=embedding
        )
        self.storage[mem_id] = record
        return record

    # -------------------------------------------------------------
    # 2. 更新与版本迭代 (Update / Supersede - SCD Type 2)
    # -------------------------------------------------------------
    def update_supersede(self, old_mem_id: str, new_value: str, 
                         new_embedding: List[float], confidence: float = 1.0) -> MemoryRecord:
        """软失效旧版本，自增版本号插入新记录，避免物理覆盖"""
        old_record = self.storage[old_mem_id]
        now = datetime.now(timezone.utc)
        
        # 旧版本归档关闭
        old_record.is_active = False
        old_record.valid_to = now
        
        # 写入新版本
        new_id = f"mem_{uuid.uuid4().hex[:8]}"
        new_record = MemoryRecord(
            memory_id=new_id,
            user_id=old_record.user_id,
            subject=old_record.subject,
            predicate=old_record.predicate,
            object_value=new_value,
            memory_type=old_record.memory_type,
            confidence=confidence,
            embedding=new_embedding,
            version=old_record.version + 1,
            created_at=now,
            last_accessed_at=now
        )
        self.storage[new_id] = new_record
        return new_record

    # -------------------------------------------------------------
    # 3. 删除 / 淘汰 (Delete / Evict)
    # -------------------------------------------------------------
    def delete(self, mem_id: str, hard_delete: bool = False):
        """支持软删除(合规/安全)与物理删除"""
        if mem_id not in self.storage:
            return
        if hard_delete:
            del self.storage[mem_id]
        else:
            self.storage[mem_id].is_active = False
            self.storage[mem_id].valid_to = datetime.now(timezone.utc)

    # -------------------------------------------------------------
    # 4. 合并与压缩 (Consolidate / Merge)
    # -------------------------------------------------------------
    def consolidate(self, target_mem_ids: List[str], generalized_value: str, 
                    new_embedding: List[float]) -> MemoryRecord:
        """将多条细碎的情景经验合并蒸馏为单条高阶抽象规则"""
        records = [self.storage[mid] for mid in target_mem_ids if mid in self.storage]
        if not records:
            raise ValueError("没有可合并的有效记录")
            
        base = records[0]
        now = datetime.now(timezone.utc)
        
        # 批量软下线碎片记忆
        for r in records:
            r.is_active = False
            r.valid_to = now
            
        # 写入合并后的抽象规则 (升阶为 SEMANTIC 语义记忆)
        new_id = f"mem_{uuid.uuid4().hex[:8]}"
        consolidated = MemoryRecord(
            memory_id=new_id,
            user_id=base.user_id,
            subject=base.subject,
            predicate="consolidated_sop",
            object_value=generalized_value,
            memory_type="SEMANTIC",
            confidence=min(1.0, sum(r.confidence for r in records) / len(records) + 0.1),
            embedding=new_embedding,
            created_at=now,
            last_accessed_at=now
        )
        self.storage[new_id] = consolidated
        return consolidated

    # -------------------------------------------------------------
    # 5. 检索与召回 (Retrieve / Recall)
    # -------------------------------------------------------------
    def retrieve(self, user_id: str, query_embedding: List[float], 
                 subject_filter: Optional[str] = None, top_k: int = 3, 
                 sim_threshold: float = 0.70) -> List[Tuple[MemoryRecord, float]]:
        """结合余弦相似度、置信度与时间衰减的复合混合检索"""
        scored_candidates = []
        now = datetime.now(timezone.utc)

        for record in self.storage.values():
            # 状态隔离门禁
            if not record.is_active or record.user_id != user_id:
                continue
            if subject_filter and record.subject != subject_filter:
                continue

            sim = cosine_similarity(query_embedding, record.embedding)
            if sim < sim_threshold:
                continue

            # 综合计算记忆排名得分 (相关性 0.6 + 置信度 0.2 + 艾宾浩斯抗衰度 0.2)
            days_passed = (now - record.last_accessed_at).total_seconds() / 86400.0
            time_decay = math.exp(-0.02 * days_passed)
            rank_score = (sim * 0.6) + (record.confidence * 0.2) + (time_decay * 0.2)
            
            scored_candidates.append((record, rank_score))

        # 按最终排分倒序截取
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        return scored_candidates[:top_k]

    # -------------------------------------------------------------
    # 6. 反哺与突触强化 (Touch / Reinforce)
    # -------------------------------------------------------------
    def touch_reinforce(self, memory_id: str, task_succeeded: bool = True):
        """当记忆被召回并成功指导任务时，强化其突触权重；若被证伪则弱化"""
        if memory_id not in self.storage:
            return
        record = self.storage[memory_id]
        record.access_count += 1
        record.last_accessed_at = datetime.now(timezone.utc)

        if task_succeeded:
            # 强化置信度与存活力
            record.confidence = min(1.0, record.confidence + 0.05)
        else:
            # 削弱置信度，降至阈值下自动失活
            record.confidence = max(0.0, record.confidence - 0.25)
            if record.confidence < 0.35:
                self.delete(memory_id, hard_delete=False)

    # -------------------------------------------------------------
    # 7. 遗忘与新陈代谢 (Decay / Forget)
    # -------------------------------------------------------------
    def run_decay_cycle(self, half_life_days: float = 30.0, retention_floor: float = 0.2):
        """定时任务：模拟大脑睡眠时的遗忘代谢机制，自动软失效边缘低活记忆"""
        now = datetime.now(timezone.utc)
        for record in self.storage.values():
            if not record.is_active or record.memory_type == "PROFILE":
                # 用户画像属于长久静态事实，不参与自动衰减淘汰
                continue

            days_idle = (now - record.last_accessed_at).total_seconds() / 86400.0
            # 活力度评分 = confidence * (1 + ln(1 + 访问次数)) * e^(-λ * t)
            lambda_param = math.log(2) / half_life_days
            vitality = record.confidence * (1.0 + math.log1p(record.access_count)) * math.exp(-lambda_param * days_idle)

            if vitality < retention_floor:
                # 活力耗尽，进入沉睡失活状态
                record.is_active = False
                record.valid_to = now

    # -------------------------------------------------------------
    # 8. 唤醒与冷激活 (Wake / Re-activate)
    # -------------------------------------------------------------
    def wake_reactivate(self, memory_id: str):
        """当外部深度检索命中已失活的冷记忆时，将其重新唤醒回活跃区"""
        if memory_id in self.storage:
            record = self.storage[memory_id]
            record.is_active = True
            record.valid_to = None
            record.confidence = 0.6  # 初始恢复给中等置信度
            record.last_accessed_at = datetime.now(timezone.utc)

```

---

### 全操作协同运转图解

```
                   [ 1. Insert (初始落盘) ]
                              │
                              ▼
            ┌─────── [ 5. Retrieve (混合检索) ] ◄────────┐
            │                 │                          │
            │ 命中并辅助成功   │ 发现新事实产生冲突        │ 长期无人访问
            ▼                 ▼                          │
[ 6. Touch/Reinforce ]  [ 2. Update/Supersede ]   [ 7. Decay/Forget ]
(增加计数, 强化置信度)   (旧版软失效, 派生版本+1)   (活力归零, 软失效沉睡)
                              │                          │
                              │ 碎片聚集                 │ 命中深潜检索
                              ▼                          ▼
                        [ 4. Consolidate ]        [ 8. Wake/Reactivate ]
                        (多条蒸馏合并为 SOP)       (重新激活赋予新生命周期)
                              │
                              ▼
                        [ 3. Delete ]
                        (合规要求强制抹除)

```

通过这 8 个 API，Agent 的记忆库跳出了单纯的数据库增删改，形成了“感知提取 $\to$ 检索强化 $\to$ 冲突迭代 $\to$ 离线合并 $\to$ 代谢淘汰”的闭环有机体。