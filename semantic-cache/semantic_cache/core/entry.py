"""核心领域模型（设计说明书 §26 / §40 / §48）。

- Message / ChatRequest ：一次 LLM 请求
- CacheEntry            ：缓存条目（§26）
- SearchResult          ：向量检索的候选结果
- CacheResult           ：最终 HIT / MISS 结果（§40 引入 confidence）
- NormalizedRequest     ：Normalizer 的产物，携带精确指纹与 system 指纹
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Message:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class ChatRequest:
    """一次 LLM 请求的领域模型。

    Cache Key 的设计要点（§8 / §21）：
      - messages 必须完整保留（system prompt 差异不能错误命中）
      - model / temperature 参与指纹
      - tenant_id 作为搜索时的硬过滤条件（§20 租户隔离）
    """

    user_id: str
    model: str
    messages: list[Message]
    tenant_id: str = "default"
    namespace: str = "semantic-cache"
    temperature: float = 0.0
    tools: list[dict[str, Any]] | None = None
    knowledge_version: str | None = None
    # Agent Cache Scope（§38）：agent_type + task_type + context_version
    agent_type: str | None = None
    task_type: str | None = None
    context_version: str | None = None
    time_sensitive: bool = False  # 实时问题（现在几点/天气/订单状态）不可缓存（§35）

    # ---- 便捷访问 ------------------------------------------------------

    @property
    def system_text(self) -> str:
        return "\n".join(m.content for m in self.messages if m.role == "system")

    @property
    def user_text(self) -> str:
        """用于 Embedding 的用户侧文本：非 system 消息拼接（通常是最后一个 user）。"""
        return "\n".join(m.content for m in self.messages if m.role != "system")

    @property
    def has_tools(self) -> bool:
        return bool(self.tools)

    # ---- 序列化 --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "user_id": self.user_id,
            "model": self.model,
            "messages": [m.to_dict() for m in self.messages],
            "tenant_id": self.tenant_id,
            "namespace": self.namespace,
            "temperature": self.temperature,
            "knowledge_version": self.knowledge_version,
            "agent_type": self.agent_type,
            "task_type": self.task_type,
            "context_version": self.context_version,
            "time_sensitive": self.time_sensitive,
        }
        if self.tools is not None:
            d["tools"] = self.tools
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatRequest":
        messages = [Message(role=m.get("role", ""), content=m.get("content", "")) for m in data.get("messages", [])]
        return cls(
            user_id=data.get("user_id", ""),
            model=data.get("model", ""),
            messages=messages,
            tenant_id=data.get("tenant_id", "default"),
            namespace=data.get("namespace", "semantic-cache"),
            temperature=data.get("temperature", 0.0),
            tools=data.get("tools"),
            knowledge_version=data.get("knowledge_version"),
            agent_type=data.get("agent_type"),
            task_type=data.get("task_type"),
            context_version=data.get("context_version"),
            time_sensitive=data.get("time_sensitive", False),
        )


@dataclass(slots=True)
class CacheEntry:
    """缓存条目（§26）。

    字段比 §26 多出的部分：
      - namespace        ：缓存域隔离
      - fingerprint      ：精确缓存 key（§9），与 exact store 一一对应
      - system_fingerprint：语义命中后 Safety 校验 system prompt（§8 / §18）
      - temperature      ：Safety 校验（§19）
      - agent_type / task_type：Agent Cache Scope（§38）
    """

    cache_id: str
    tenant_id: str
    model: str
    prompt: str  # 规范化后的用户问题
    embedding: list[float]
    response: dict[str, Any]
    created_at: float
    expires_at: float
    namespace: str = "semantic-cache"
    fingerprint: str = ""
    system_fingerprint: str | None = None
    temperature: float = 0.0
    knowledge_version: str | None = None
    agent_type: str | None = None
    task_type: str | None = None
    context_version: str | None = None
    hit_count: int = 0

    # ---- TTL -----------------------------------------------------------

    @property
    def expired(self) -> bool:
        return self.expires_at <= time.time()

    def expires_in(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, self.expires_at - now)

    def touch_hit(self) -> None:
        """命中一次，hit_count +1（§41 cache_eviction 观察用）。"""
        self.hit_count += 1

    # ---- 序列化 --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_id": self.cache_id,
            "namespace": self.namespace,
            "tenant_id": self.tenant_id,
            "model": self.model,
            "fingerprint": self.fingerprint,
            "system_fingerprint": self.system_fingerprint,
            "prompt": self.prompt,
            "embedding": self.embedding,
            "response": self.response,
            "temperature": self.temperature,
            "knowledge_version": self.knowledge_version,
            "agent_type": self.agent_type,
            "task_type": self.task_type,
            "context_version": self.context_version,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "hit_count": self.hit_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CacheEntry":
        return cls(**data)


@dataclass(slots=True)
class SearchResult:
    """向量检索候选（§13 Top-K 结果）。"""

    entry: CacheEntry
    similarity: float


@dataclass(slots=True)
class CacheResult:
    """一次缓存查询的结果（§40：hit + similarity + confidence）。"""

    hit: bool
    response: dict[str, Any] | None = None
    similarity: float = 0.0
    confidence: float = 0.0
    source: str = "miss"  # "exact" | "semantic" | "miss"
    entry: CacheEntry | None = field(default=None, repr=False)

    @classmethod
    def exact_hit(cls, entry: CacheEntry) -> "CacheResult":
        return cls(hit=True, response=entry.response, similarity=1.0, confidence=1.0, source="exact", entry=entry)

    @classmethod
    def semantic_hit(
        cls, entry: CacheEntry, similarity: float, confidence: float | None = None
    ) -> "CacheResult":
        return cls(
            hit=True,
            response=entry.response,
            similarity=round(similarity, 4),
            confidence=round(similarity if confidence is None else confidence, 4),
            source="semantic",
            entry=entry,
        )

    @classmethod
    def miss(cls) -> "CacheResult":
        return cls(hit=False, source="miss")

    def to_dict(self) -> dict[str, Any]:
        return {
            "hit": self.hit,
            "source": self.source,
            "similarity": self.similarity,
            "confidence": self.confidence,
        }
