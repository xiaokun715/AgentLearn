"""第 3 层 · 情景记忆 (Episodic Memory) —— 自传式经验归档 + Top-K RAG。

对齐说明书 §情景记忆：

* **离线沉淀**：任务完成后后台蒸馏成 ``EPISODIC`` 事实(见 ``worker.distill``)，本类负责落库；
* **动态检索(Top-K RAG)**：新任务进来时，把 query 编码成向量走 ``engine.retrieve``，
  召回最相似的几条历史经验作为 Few-Shot 注入工作记忆；
* **时效衰减**：EPISODIC 参与 Decay 代谢(引擎层面已实现，见 ``run_decay_cycle``)。

底层就复用引擎；本层只做“把情景经验的语义写进去 / 取出来”。
"""
from __future__ import annotations

from typing import Optional

from ..engine import MemoryEngine
from ..records import EPISODIC, MemoryRecord, MemoryType


class EpisodicMemory:
    def __init__(
        self,
        engine: MemoryEngine,
        top_k: int = 3,
        sim_threshold: float = 0.30,
    ) -> None:
        self.engine = engine
        self.top_k = top_k
        self.sim_threshold = sim_threshold

    # ------------------------------------------------------------------
    def save_fact(
        self,
        user_id: str,
        subject: str,
        predicate: str,
        object_value: str,
        memory_type: MemoryType = EPISODIC,
        confidence: float = 1.0,
    ) -> MemoryRecord:
        """把一条沉淀下来的经验写入情景/语义记忆(向量自动编码)。"""
        return self.engine.remember(
            user_id=user_id,
            subject=subject,
            predicate=predicate,
            object_value=object_value,
            memory_type=memory_type,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    def recall(
        self,
        query: str,
        user_id: str,
        top_k: Optional[int] = None,
        sim_threshold: Optional[float] = None,
    ) -> list[tuple[MemoryRecord, float]]:
        """对 query 做语义检索，召回最相似的历史经验(few-shot 素材)。

        ``top_k`` 与 ``sim_threshold`` 缺省取本层(由 config 装配)的调优值；
        注意：引擎方法默认阈值 0.70 是为真实嵌入保留的，本层用词袋嵌入所以默认更低。
        """
        q_vec = self.engine.embedder.embed(query)
        return self.engine.retrieve(
            user_id=user_id,
            query_embedding=q_vec,
            top_k=top_k or self.top_k,
            sim_threshold=(
                sim_threshold if sim_threshold is not None else self.sim_threshold
            ),
        )

    def few_shots(self, query: str, user_id: str) -> list[str]:
        """把召回的几条经验格式化成可直接注入工作记忆的 few-shot 行。"""
        return [
            f"回忆：{r.subject} | {r.object_value}"
            for r, _ in self.recall(query, user_id)
        ]

    def recall_sleeping(self, query: str, user_id: str, top_k: int = 5) -> list[tuple[MemoryRecord, float]]:
        """深潜检索沉睡冷记忆(命中即可 wake 复活)。"""
        q_vec = self.engine.embedder.embed(query)
        return self.engine.deep_retrieve(user_id=user_id, query_embedding=q_vec, top_k=top_k)

    # ------------------------------------------------------------------
    def episodes(self, user_id: str | None = None) -> list[MemoryRecord]:
        """当前活跃的情景记忆(便于观测)。"""
        return [
            r
            for r in self.engine.active_memories(user_id)
            if r.memory_type == EPISODIC
        ]

    def snapshot(self, user_id: str | None = None) -> dict[str, int]:
        """按记忆类型统计该层规模(观测 API 用)。"""
        by_type: dict[str, int] = {}
        for r in self.engine.active_memories(user_id):
            by_type[r.memory_type] = by_type.get(r.memory_type, 0) + 1
        return by_type
