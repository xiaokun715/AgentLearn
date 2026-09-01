"""依赖装配（设计说明书 §47 / §48）。

从 ``CacheConfig`` 组装出完整可用的 SemanticCache，
供 main.py（HTTP 服务）与 examples / experiments 复用。
"""
from __future__ import annotations

from .config import CacheConfig
from .core.cache import SemanticCache
from .core.policy import CachePolicy, ThresholdPolicy
from .embedding.base import EmbeddingGenerator
from .embedding.mock import MockEmbeddingGenerator
from .metrics.metrics import CacheMetrics
from .normalize.normalizer import PromptNormalizer
from .safety.validator import SafetyValidator
from .storage.base import CacheStore
from .storage.memory import MemoryStore


def build_store(config: CacheConfig, *, on_expired=None) -> CacheStore:
    """存储后端：memory（默认）或 postgres + pgvector（§28）。"""
    if config.store_backend == "postgres":
        from .storage.postgres import PostgresStore

        if not config.database_url:
            raise ValueError("store_backend=postgres 需要设置 DATABASE_URL 环境变量")
        return PostgresStore(config.database_url)
    return MemoryStore(on_expired=on_expired)


def build_embedding(config: CacheConfig) -> EmbeddingGenerator:
    """Embedding 后端：mock（默认）或 qwen（§11，需 DASHSCOPE_API_KEY）。"""
    if config.embedding_provider == "qwen":
        from .embedding.qwen import QwenEmbeddingGenerator

        return QwenEmbeddingGenerator(
            api_key=config.qwen_api_key,
            base_url=config.qwen_base_url,
            model=config.qwen_model,
            dim=config.dim,
        )
    return MockEmbeddingGenerator(dim=config.dim)


def build_cache(
    config: CacheConfig | None = None,
    *,
    store: CacheStore | None = None,
    embedding: EmbeddingGenerator | None = None,
    metrics: CacheMetrics | None = None,
    threshold: float | None = None,
) -> SemanticCache:
    config = config or CacheConfig.from_env()
    metrics = metrics or CacheMetrics(
        input_cost_per_million=config.input_cost_per_million,
        output_cost_per_million=config.output_cost_per_million,
    )
    store = store or build_store(config, on_expired=metrics.record_expiration)
    embedding = embedding or build_embedding(config)

    return SemanticCache(
        store=store,
        normalizer=PromptNormalizer(),
        embedding=embedding,
        threshold_policy=ThresholdPolicy(threshold=config.threshold if threshold is None else threshold),
        cache_policy=CachePolicy(max_temperature=config.max_temperature),
        validator=SafetyValidator(),
        metrics=metrics,
        default_ttl=config.default_ttl,
        top_k=config.top_k,
        confidence_margin=config.confidence_margin,
    )
