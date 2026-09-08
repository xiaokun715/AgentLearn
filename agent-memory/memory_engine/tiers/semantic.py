"""第 4 层 · 语义记忆 + 用户画像 (Semantic & Profile) —— 确定性知识层。

对齐说明书 §语义记忆与用户画像 与 §热注水：

* **ProfileStore(用户画像)**：模拟 Redis Hash。确定性偏好(语言/集群/编码习惯)走
  **点查全量静态加载**，不走向量检索 —— 100% 命中、零检索耗时(说明书“确定性配置走点查”)。
* **SemanticMemory(语义/规则)**：客观知识、SOP、领域规律。这里也支持按 subject 点查，
  以及承接 Consolidate 升阶出的 ``SEMANTIC`` 记忆(写经引擎即可，本类只提供更顺手的门面)。
"""
from __future__ import annotations

from typing import Any

from ..engine import MemoryEngine
from ..records import SEMANTIC, MemoryRecord


class ProfileStore:
    """用户画像：dict 模拟 Redis Hash，按 user_id 全量静态存取。"""

    def __init__(self, seed: dict[str, dict[str, str]] | None = None) -> None:
        self.profiles: dict[str, dict[str, str]] = dict(seed or {})

    def set(self, user_id: str, key: str, value: str) -> None:
        self.profiles.setdefault(user_id, {})[key] = value

    def set_many(self, user_id: str, kv: dict[str, str]) -> None:
        self.profiles.setdefault(user_id, {}).update(kv)

    def get(self, user_id: str) -> dict[str, str]:
        """点查(Redis HGETALL 语义)：全量取出，命中即返回，零检索。"""
        return dict(self.profiles.get(user_id, {}))

    def as_lines(self, user_id: str) -> list[str]:
        """把画像渲染成注入工作记忆的静态行(请求进来时全量热加载)。"""
        return [f"{k}: {v}" for k, v in self.get(user_id).items()]


class SemanticMemory:
    """确定性语义/规则记忆门面：点查 + 承接 Consolidate 的 SEMANTIC 产出。"""

    def __init__(self, engine: MemoryEngine) -> None:
        self.engine = engine

    def save_rule(
        self, user_id: str, subject: str, object_value: str, predicate: str = "规则"
    ) -> MemoryRecord:
        return self.engine.remember(
            user_id=user_id,
            subject=subject,
            predicate=predicate,
            object_value=object_value,
            memory_type=SEMANTIC,
        )

    def point_query(self, user_id: str, subject: str) -> list[MemoryRecord]:
        """按 subject 精确过滤(不走向量) —— 说明书“确定性知识点查，杜绝漏召回”。"""
        return [
            r
            for r in self.engine.active_memories(user_id)
            if r.subject == subject and r.memory_type == SEMANTIC
        ]

    def point_query_by_text(self, user_id: str, query: str) -> list[MemoryRecord]:
        """用 query 文本对“规则 subject”做确定性子串命中(不走向量近似)。

        规则库规模小且 subject 稳定(如『基带告警』)，直接子串匹配即可精确命中，
        属于“确定性点查”而非向量召回 —— 避免少量规则被向量检索漏掉。
        """
        hits = []
        for r in self.semantic_rules(user_id):
            if r.subject and r.subject in query:
                hits.append(r)
        return hits

    def semantic_rules(self, user_id: str | None = None) -> list[MemoryRecord]:
        return self.all_rules(user_id)

    def all_rules(self, user_id: str | None = None) -> list[MemoryRecord]:
        return [
            r
            for r in self.engine.active_memories(user_id)
            if r.memory_type == SEMANTIC
        ]

    def as_context(self, user_id: str, subject: str) -> list[str]:
        return [f"规则[{r.subject}]: {r.object_value}" for r in self.point_query(user_id, subject)]
