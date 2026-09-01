"""SemanticCache 核心（设计说明书 §32 / §33 / §49 ~ §50）。

完整流程（§32）：
  Request -> Normalize -> Exact Fingerprint
    ├── Exact HIT -> Return
    ▼
  Embedding -> Vector Search(Top-K) -> Threshold
    ├── < threshold -> MISS
    ▼
  Safety Validator
    ├── FAIL -> MISS
    ▼
  Cache HIT -> Return Cached Response

为什么先 Exact 再 Semantic（§33）：
  Exact Match = Hash + O(1) 读取，几乎零成本；
  Semantic 需要 Embedding + 向量检索 + 相似度计算，本身有成本。
  所以顺序必须是 Exact -> Semantic -> LLM。
"""
from __future__ import annotations

import time
import uuid

from ..core.entry import CacheEntry, CacheResult, ChatRequest
from ..core.policy import CachePolicy, ThresholdPolicy
from ..embedding.base import EmbeddingGenerator
from ..invalidation.manager import InvalidationManager
from ..metrics.metrics import CacheMetrics
from ..normalize.normalizer import PromptNormalizer
from ..safety.validator import SafetyValidator
from ..storage.base import CacheStore


class SemanticCache:
    def __init__(
        self,
        *,
        store: CacheStore,
        normalizer: PromptNormalizer,
        embedding: EmbeddingGenerator,
        threshold_policy: ThresholdPolicy,
        cache_policy: CachePolicy,
        validator: SafetyValidator,
        metrics: CacheMetrics | None = None,
        default_ttl: int = 3600,
        top_k: int = 5,
        confidence_margin: float = 0.02,
    ):
        self.store = store
        self.normalizer = normalizer
        self.embedding = embedding
        self.threshold_policy = threshold_policy
        self.cache_policy = cache_policy
        self.validator = validator
        self.metrics = metrics or CacheMetrics()
        self.default_ttl = default_ttl
        self.top_k = top_k
        self.confidence_margin = confidence_margin
        self.invalidation = InvalidationManager(store)

    # ---- 读路径（§49）--------------------------------------------------

    async def get(self, request: ChatRequest) -> CacheResult:
        self.metrics.record_request()
        started = time.perf_counter()

        # 1. Normalize（§6）
        normalized = self.normalizer.normalize_request(request)

        # 2. Exact cache（§33：成本最低，先查；命中即返回）
        exact = await self.store.get_exact(
            namespace=request.namespace,
            tenant_id=request.tenant_id,
            model=request.model,
            fingerprint=normalized.fingerprint,
        )
        if exact is not None:
            exact.touch_hit()
            self._finalize_hit("exact", 1.0, exact.response, started)
            return CacheResult.exact_hit(exact)

        # 3. Embedding（§10）
        vector = await self.embedding.embed(normalized.user_text)

        # 4. Vector search Top-K（§12 ~ §13），store 已做 租户/模型/TTL 过滤（§31）
        candidates = await self.store.search(
            vector,
            namespace=request.namespace,
            tenant_id=request.tenant_id,
            model=request.model,
            top_k=self.top_k,
        )

        # 5. Threshold policy（§15）
        selected = self.threshold_policy.select(candidates, request=request)
        if selected is None:
            self._finalize_miss(started)
            return CacheResult.miss()

        # 6. Safety validator（§18：模型/temperature/system prompt/版本/Agent scope）
        valid, _reasons = self.validator.validate(
            request,
            selected.entry,
            system_fingerprint=normalized.system_fingerprint,
        )
        if not valid:
            self._finalize_miss(started)
            return CacheResult.miss()

        # 7. HIT（§40：携带 similarity + confidence）
        entry = selected.entry
        entry.touch_hit()
        similarity = selected.similarity
        confidence = max(0.0, min(1.0, similarity - self.confidence_margin))
        self._finalize_hit("semantic", similarity, entry.response, started)
        return CacheResult.semantic_hit(entry, similarity, confidence=confidence)

    # ---- 写路径（§50）--------------------------------------------------

    async def set(
        self,
        request: ChatRequest,
        response: dict,
        *,
        ttl: int | None = None,
        knowledge_version: str | None = None,
    ) -> bool:
        """写缓存（§34：只有确定答案可缓存时才写）。

        返回是否成功写入（False = 被 Cacheability Policy 拒绝，§36 / §35）。
        """
        cacheable, reason = self.cache_policy.is_cacheable(request, response)
        if not cacheable:
            self.metrics.record_skipped(reason)
            return False

        normalized = self.normalizer.normalize_request(request)
        vector = await self.embedding.embed(normalized.user_text)

        now = time.time()
        entry = CacheEntry(
            cache_id=str(uuid.uuid4()),
            namespace=request.namespace,
            tenant_id=request.tenant_id,
            model=request.model,
            fingerprint=normalized.fingerprint,
            system_fingerprint=normalized.system_fingerprint,
            prompt=normalized.user_text,
            embedding=vector,
            response=response,
            temperature=request.temperature,
            knowledge_version=knowledge_version or request.knowledge_version,
            agent_type=request.agent_type,
            task_type=request.task_type,
            context_version=request.context_version,
            created_at=now,
            expires_at=now + (ttl if ttl is not None else self.default_ttl),
        )
        await self.store.insert(entry)
        self.metrics.record_set()
        return True

    # ---- 失效（§24 ~ §25）----------------------------------------------

    async def invalidate(
        self,
        *,
        cache_id: str | None = None,
        namespace: str | None = None,
        tenant_id: str | None = None,
        model: str | None = None,
        knowledge_version: str | None = None,
        agent_type: str | None = None,
        task_type: str | None = None,
    ) -> int:
        """主动失效，返回删除条数。"""
        deleted = await self.invalidation.invalidate(
            cache_id=cache_id,
            namespace=namespace,
            tenant_id=tenant_id,
            model=model,
            knowledge_version=knowledge_version,
            agent_type=agent_type,
            task_type=task_type,
        )
        if deleted:
            self.metrics.record_eviction(deleted)
        return deleted

    # ---- 统计 ----------------------------------------------------------

    async def stats(self) -> dict:
        size = await self.store.count(namespace=None, tenant_id=None)
        snapshot = self.metrics.snapshot()
        snapshot["cache_size"] = size
        return snapshot

    # ---- 内部指标 ------------------------------------------------------

    def _finalize_hit(self, source: str, similarity: float, response: dict, started: float) -> None:
        self.metrics.record_hit(source, response=response)  # 顺便累计 Token/Cost Saved（§44）
        self.metrics.record_similarity(similarity)
        self.metrics.record_latency((time.perf_counter() - started) * 1000)

    def _finalize_miss(self, started: float) -> None:
        self.metrics.record_miss()
        self.metrics.record_latency((time.perf_counter() - started) * 1000)
