"""Safety Validator（设计说明书 §18 ~ §20 / §25 / §38）。

相似度 HIT 之后、真正 HIT 之前的一道硬校验：

  Similarity HIT -> Safety Validator -> 真正 HIT

它解决「语义相似 ≠ 可以复用答案」的风险（§17 / §39）：
  - 不同 model 的答案不能互相复用（§19）
  - 不同 temperature 的答案不能互相复用（§19）
  - 不同 system prompt 的答案不能互相复用（§8）
  - 不同知识库版本（knowledge_version）的答案不能互相复用（§25）
  - 不同 Agent 类型/任务类型的答案不能互相复用（§38）
  - tenant 隔离由 Store 的 WHERE 过滤承担，这里做双保险（§20）
"""
from __future__ import annotations

from ..core.entry import CacheEntry, ChatRequest

_TEMPERATURE_EPSILON = 1e-6


class SafetyValidator:
    def __init__(
        self,
        *,
        require_same_system: bool = True,
        require_same_knowledge_version: bool = True,
        temperature_epsilon: float = _TEMPERATURE_EPSILON,
    ):
        self.require_same_system = require_same_system
        self.require_same_knowledge_version = require_same_knowledge_version
        self.temperature_epsilon = temperature_epsilon

    def validate(
        self,
        request: ChatRequest,
        entry: CacheEntry,
        *,
        system_fingerprint: str | None = None,
    ) -> tuple[bool, list[str]]:
        """校验缓存条目是否可用于本次请求。

        返回 ``(通过?, 未通过原因列表)``。原因为空表示通过。
        ``system_fingerprint`` 由 SemanticCache 传入（Normalizer 已算好）。
        """
        reasons: list[str] = []

        # —— 请求侧基础字段（§19）——
        if request.model != entry.model:
            reasons.append("model")
        if abs(request.temperature - entry.temperature) > self.temperature_epsilon:
            reasons.append("temperature")
        if request.namespace != entry.namespace:
            reasons.append("namespace")

        # tenant：正常由 Store 的 WHERE 过滤保证（§20），此处双保险
        if request.tenant_id != entry.tenant_id:
            reasons.append("tenant")

        # —— system prompt（§8）——
        if (
            self.require_same_system
            and system_fingerprint is not None
            and entry.system_fingerprint is not None
            and system_fingerprint != entry.system_fingerprint
        ):
            reasons.append("system_prompt")

        # —— 知识库版本（§25）——
        if (
            self.require_same_knowledge_version
            and request.knowledge_version is not None
            and entry.knowledge_version != request.knowledge_version
        ):
            reasons.append("knowledge_version")

        # —— Agent Cache Scope（§38）：agent_type / task_type / context_version ——
        if not self._scope_match(request.agent_type, entry.agent_type):
            reasons.append("agent_type")
        if not self._scope_match(request.task_type, entry.task_type):
            reasons.append("task_type")
        if not self._scope_match(request.context_version, entry.context_version):
            reasons.append("context_version")

        return (len(reasons) == 0, reasons)

    @staticmethod
    def _scope_match(request_value: str | None, entry_value: str | None) -> bool:
        """任一方向未声明该维度时放宽（由 namespace 承担粗隔离）；都声明则必须相等。"""
        if request_value is None or entry_value is None:
            return True
        return request_value == entry_value

    def to_dict(self) -> dict[str, bool]:
        return {
            "require_same_system": self.require_same_system,
            "require_same_knowledge_version": self.require_same_knowledge_version,
        }
