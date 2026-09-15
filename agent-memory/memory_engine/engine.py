"""记忆引擎：说明书 8 种核心原语。

对齐《第十一章：Agent 记忆系统设计说明书》§核心操作的 Python 工程实现里的
``ProductionAgentMemoryEngine``，方法名/语义逐一对应：

1. Insert / Form            ``insert``          —— 初始写入一条新事实
2. Update / Supersede(SCD2) ``update_supersede`` —— 旧版软失效归档, 版本号 +1 派生新记录
3. Delete / Evict           ``delete``          —— 软删除(合规)/物理抹除(硬删)
4. Consolidate / Merge      ``consolidate``     —— 多条碎片蒸馏合并为 SEMANTIC SOP
5. Retrieve / Recall        ``retrieve``        —— 余弦相似度+置信度+艾宾浩斯抗衰 混合打分
6. Touch / Reinforce        ``touch_reinforce`` —— 召回后强化突触权重 / 证伪降权自动失活
7. Decay / Forget           ``run_decay_cycle`` —— 活力评分代谢, 低活记忆软失效沉睡
8. Wake / Re-activate       ``wake_reactivate`` —— 深度检索命中冷记忆后唤醒复活

底层 ``storage: dict`` 模拟 PostgreSQL + pgvector：把引擎当黑盒换掉这张 dict 即接入真实存储。
"""
from __future__ import annotations

import math
import uuid
from datetime import timedelta
from typing import Iterable, Optional

from .embedding import KeywordEmbedder, cosine_similarity
from .records import EPISODIC, PROFILE, SEMANTIC, MemoryRecord, MemoryType, utcnow


def _new_id() -> str:
    """生成 ``mem_xxxxxxxx`` 形式的记忆 ID(说明书同款)。"""
    return f"mem_{uuid.uuid4().hex[:8]}"


class MemoryEngine:
    """8 原语记忆引擎。文档字符串给出的示例即说明书语义复刻。"""

    def __init__(self, embedder: Optional[KeywordEmbedder] = None) -> None:
        # 模拟底层存储 (PostgreSQL + pgvector)
        self.storage: dict[str, MemoryRecord] = {}
        self.embedder: KeywordEmbedder = embedder or KeywordEmbedder(dim=256)

    # ------------------------------------------------------------------
    # 1. 添加 (Insert / Form)
    # ------------------------------------------------------------------
    def insert(
        self,
        user_id: str,
        subject: str,
        predicate: str,
        object_value: str,
        memory_type: MemoryType = SEMANTIC,
        embedding: Optional[list[float]] = None,
    ) -> MemoryRecord:
        """初始写入一条新事实；embedding 缺省时用 embedder 对 object_value 自动编码。"""
        mem_id = _new_id()
        record = MemoryRecord(
            memory_id=mem_id,
            user_id=user_id,
            subject=subject,
            predicate=predicate,
            object_value=object_value,
            memory_type=memory_type,
            embedding=(
                embedding
                if embedding is not None
                else self.embedder.embed(object_value)
            ),
        )
        self.storage[mem_id] = record
        return record

    # 便捷别名：自动编码事实文本(API / 演示脚本最常用)
    def remember(
        self,
        user_id: str,
        subject: str,
        predicate: str,
        object_value: str,
        memory_type: MemoryType = SEMANTIC,
        confidence: float = 1.0,
        embedding: Optional[list[float]] = None,
    ) -> MemoryRecord:
        """插入并返回(embedding 缺省用「subject·predicate·object_value」文档编码)。

        ``insert``(忠于说明书)在无 embedding 时只编码 object_value；本糖方法为了词袋嵌入
        也能稳定召回，默认编码整个三元组 —— 类似真实 RAG 索引“标题+正文”。
        """
        if embedding is None:
            embedding = self.embedder.embed(
                f"{subject} {predicate} {object_value}"
            )
        record = self.insert(user_id, subject, predicate, object_value, memory_type, embedding)
        record.confidence = confidence
        return record

    # ------------------------------------------------------------------
    # 2. 更新与版本迭代 (Update / Supersede - SCD Type 2)
    # ------------------------------------------------------------------
    def update_supersede(
        self,
        old_mem_id: str,
        new_value: str,
        new_embedding: Optional[list[float]] = None,
        confidence: float = 1.0,
    ) -> MemoryRecord:
        """软失效旧版本，自增版本号插入新记录，避免物理覆盖(SCD Type 2)。"""
        old_record = self._require(old_mem_id)
        now = utcnow()

        # 旧版本归档关闭
        old_record.is_active = False
        old_record.valid_to = now

        # 写入新版本
        new_record = MemoryRecord(
            memory_id=_new_id(),
            user_id=old_record.user_id,
            subject=old_record.subject,
            predicate=old_record.predicate,
            object_value=new_value,
            memory_type=old_record.memory_type,
            confidence=confidence,
            embedding=(
                new_embedding
                if new_embedding is not None
                else self.embedder.embed(new_value)
            ),
            version=old_record.version + 1,
            created_at=now,
            last_accessed_at=now,
        )
        self.storage[new_record.memory_id] = new_record
        return new_record

    # ------------------------------------------------------------------
    # 3. 删除 / 淘汰 (Delete / Evict)
    # ------------------------------------------------------------------
    def delete(self, mem_id: str, hard_delete: bool = False) -> None:
        """支持软删除(合规/安全/归档)与物理删除(彻底抹除)。"""
        record = self.storage.get(mem_id)
        if record is None:
            return
        if hard_delete:
            del self.storage[mem_id]
        else:
            record.is_active = False
            record.valid_to = utcnow()

    # ------------------------------------------------------------------
    # 4. 合并与压缩 (Consolidate / Merge)
    # ------------------------------------------------------------------
    def consolidate(
        self,
        memory_ids: Iterable[str],
        generalized_value: str,
        new_embedding: Optional[list[float]] = None,
    ) -> MemoryRecord:
        """把多条细碎的情景/事实碎片合并蒸馏为一条高阶抽象规则(SOP)。

        约束：碎片必须存在、非空、且同属一个 user_id(数据完整性)；
        合并后碎片全部软下线，产出一条 ``predicate=consolidated_sop`` 的 SEMANTIC 记忆，
        置信度 = min(1.0, 碎片平均置信度 + 0.1) —— 多条互相印证的记忆比单条更可信。
        """
        unique_ids = list(dict.fromkeys(memory_ids))
        records: list[MemoryRecord] = []
        for mid in unique_ids:
            record = self.storage.get(mid)
            if record is None:
                raise ValueError(f"memory_id {mid!r} 不存在")
            records.append(record)
        if not records:
            raise ValueError("没有可合并的有效记录")
        if len({r.user_id for r in records}) != 1:
            raise ValueError("合并目标必须属于同一个 user_id")

        base = records[0]
        now = utcnow()

        # 批量软下线碎片记忆
        for r in records:
            r.is_active = False
            r.valid_to = now

        # 写入合并后的抽象规则 (升阶为 SEMANTIC 语义记忆)
        consolidated = MemoryRecord(
            memory_id=_new_id(),
            user_id=base.user_id,
            subject=base.subject,
            predicate="consolidated_sop",
            object_value=generalized_value,
            memory_type=SEMANTIC,
            confidence=min(1.0, sum(r.confidence for r in records) / len(records) + 0.1),
            embedding=(
                new_embedding
                if new_embedding is not None
                else self.embedder.embed(generalized_value)
            ),
            created_at=now,
            last_accessed_at=now,
        )
        self.storage[consolidated.memory_id] = consolidated
        return consolidated

    # ------------------------------------------------------------------
    # 5. 检索与召回 (Retrieve / Recall)
    # ------------------------------------------------------------------
    def retrieve(
        self,
        user_id: str,
        query_embedding: Optional[list[float]] = None,
        subject_filter: Optional[str] = None,
        top_k: int = 3,
        sim_threshold: float = 0.70,
    ) -> list[tuple[MemoryRecord, float]]:
        """结合余弦相似度、置信度与时间衰减的复合混合检索(只搜活跃记忆)。

        排名分 = 语义相关 0.6 × sim + 置信度 0.2 × confidence + 抗衰度 0.2 × e^(-0.02×空闲天数)，
        直接对应说明书 “相关性 0.6 + 置信度 0.2 + 艾宾浩斯抗衰度 0.2”。
        """
        scored_candidates: list[tuple[MemoryRecord, float]] = []
        now = utcnow()
        query = query_embedding if query_embedding is not None else []

        for record in self.storage.values():
            # 状态隔离门禁
            if not record.is_active or record.user_id != user_id:
                continue
            if subject_filter and record.subject != subject_filter:
                continue

            sim = cosine_similarity(query, record.embedding)
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

    def deep_retrieve(
        self,
        user_id: str,
        query_embedding: Optional[list[float]] = None,
        subject_filter: Optional[str] = None,
        top_k: int = 5,
        sim_threshold: float = 0.30,
    ) -> list[tuple[MemoryRecord, float]]:
        """“深潜检索”：连沉睡/失活冷记忆一起命中(不设活跃门禁)。

        用于说明书第 8 原语的前置场景 —— 当普通 ``retrieve`` 搜不到、但老经验其实高度相关时，
        用本方法把冷记忆捞出来再 ``wake_reactivate`` 唤醒复活。
        """
        scored: list[tuple[MemoryRecord, float]] = []
        query = query_embedding if query_embedding is not None else []
        for record in self.storage.values():
            if record.user_id != user_id:
                continue
            if subject_filter and record.subject != subject_filter:
                continue
            sim = cosine_similarity(query, record.embedding)
            if sim < sim_threshold:
                continue
            # 冷记忆默认给予衰减罚分，保证“沉睡的”排后面
            days_passed = (utcnow() - record.last_accessed_at).total_seconds() / 86400.0
            time_decay = math.exp(-0.02 * days_passed)
            score = sim * 0.6 + record.confidence * 0.2 + time_decay * 0.2
            scored.append((record, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # ------------------------------------------------------------------
    # 6. 反哺与突触强化 (Touch / Reinforce)
    # ------------------------------------------------------------------
    def touch_reinforce(self, memory_id: str, task_succeeded: bool = True) -> None:
        """记忆被召回并成功指导任务时强化突触权重；若被证伪则大幅削弱、低置信自动失活。"""
        record = self.storage.get(memory_id)
        if record is None:
            return
        record.access_count += 1
        record.last_accessed_at = utcnow()

        if task_succeeded:
            # 强化置信度与存活力
            record.confidence = min(1.0, record.confidence + 0.05)
        else:
            # 削弱置信度，降至阈值下自动失活
            record.confidence = max(0.0, record.confidence - 0.25)
            if record.confidence < 0.35:
                self.delete(memory_id, hard_delete=False)

    # ------------------------------------------------------------------
    # 7. 遗忘与新陈代谢 (Decay / Forget)
    # ------------------------------------------------------------------
    def run_decay_cycle(
        self, half_life_days: float = 30.0, retention_floor: float = 0.2
    ) -> list[MemoryRecord]:
        """定时任务：模拟大脑睡眠时的遗忘代谢，软失效边缘低活记忆(返回被沉睡的记录)。

        活力分 R = confidence × (1 + ln(1 + 访问次数)) × e^(-λ·空闲天数)，
        其中 λ = ln2 / 半衰期。PROFILE 画像属长久静态事实，不参与自动衰减(直接跳过)。
        """
        now = utcnow()
        lambda_param = math.log(2) / half_life_days
        slept: list[MemoryRecord] = []

        for record in self.storage.values():
            if not record.is_active or record.memory_type == PROFILE:
                # 用户画像属于长久静态事实，不参与自动衰减淘汰
                continue

            days_idle = (now - record.last_accessed_at).total_seconds() / 86400.0
            # 活力度评分 = confidence * (1 + ln(1 + 访问次数)) * e^(-λ * t)
            vitality = (
                record.confidence
                * (1.0 + math.log1p(record.access_count))
                * math.exp(-lambda_param * days_idle)
            )

            if vitality < retention_floor:
                # 活力耗尽，进入沉睡失活状态
                record.is_active = False
                record.valid_to = now
                slept.append(record)

        return slept

    # ------------------------------------------------------------------
    # 8. 唤醒与冷激活 (Wake / Re-activate)
    # ------------------------------------------------------------------
    def wake_reactivate(self, memory_id: str) -> MemoryRecord | None:
        """深度检索命中已失活的冷记忆时，将其重新唤醒回活跃区。

        初始恢复给中等置信度 0.6，代表“重新上岗但需要再次验证”。
        """
        record = self.storage.get(memory_id)
        if record is None:
            return None
        record.is_active = True
        record.valid_to = None
        record.confidence = 0.6  # 初始恢复给中等置信度
        record.last_accessed_at = utcnow()
        return record

    # ------------------------------------------------------------------
    # 查询/工具方法(糖，帮助 API、demo、测试可观测)
    # ------------------------------------------------------------------
    def _require(self, mem_id: str) -> MemoryRecord:
        if mem_id not in self.storage:
            raise KeyError(f"memory_id {mem_id!r} 不存在")
        return self.storage[mem_id]

    def get(self, mem_id: str) -> MemoryRecord | None:
        return self.storage.get(mem_id)

    def active_memories(self, user_id: str | None = None) -> list[MemoryRecord]:
        return [
            r
            for r in self.storage.values()
            if r.is_active and (user_id is None or r.user_id == user_id)
        ]

    def sleeping_memories(self, user_id: str | None = None) -> list[MemoryRecord]:
        return [
            r
            for r in self.storage.values()
            if not r.is_active and (user_id is None or r.user_id == user_id)
        ]

    def history(self, user_id: str, subject: str) -> list[MemoryRecord]:
        """subject 的完整版本链(按创建时间升序) —— 展示 SCD2 换代历史。"""
        chain = [
            r
            for r in self.storage.values()
            if r.user_id == user_id and r.subject == subject
        ]
        chain.sort(key=lambda r: r.created_at)
        return chain

    def stats(self) -> dict[str, int]:
        """按记忆类型 × 活跃状态做统计，观测 API 用。"""
        total = len(self.storage)
        active = sum(1 for r in self.storage.values() if r.is_active)
        sleeping = total - active
        by_type: dict[str, int] = {}
        by_state: dict[str, dict[str, int]] = {}
        for r in self.storage.values():
            by_type[r.memory_type] = by_type.get(r.memory_type, 0) + 1
            bucket = by_state.setdefault(r.memory_type, {"active": 0, "sleeping": 0})
            bucket["active" if r.is_active else "sleeping"] += 1
        return {"total": total, "active": active, "sleeping": sleeping,
                "by_type": by_type, "by_state": by_state}

    def simulate_elapsed(self, days: float, user_id: str | None = None) -> None:
        """让“整个世界”快进 days 天(教学/测试用)：所有记录的统一时钟往前走。

        具体实现是把 created_at / last_accessed_at 一起回拨 days，
        等价于记录早已写入且期间无人再访问 —— 让 Decay/Touch 的时间逻辑可被确定性观察。
        """
        delta = timedelta(days=days)
        for r in self.storage.values():
            if user_id is not None and r.user_id != user_id:
                continue
            r.created_at = r.created_at - delta
            r.last_accessed_at = r.last_accessed_at - delta
            if r.valid_to is not None:
                r.valid_to = r.valid_to - delta
