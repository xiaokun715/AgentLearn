"""SemanticCache 全局配置（设计说明书 §47）。

优先从环境变量读取，便于 docker-compose / 生产环境覆盖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class CacheConfig:
    """Semantic Cache 运行配置。"""

    # 命名空间：普通 chat 与 Agent 子任务缓存相互隔离（§38）
    namespace: str = "semantic-cache"

    # Embedding 维度（§10：dim = 1024）
    dim: int = 1024

    # 语义检索 Top-K（§13）
    top_k: int = 5

    # 相似度阈值（§15）。该值面向 Mock 向量校准，接入真实 Embedding 后
    # 必须按 §16 用正负样本对重新评估，不能简单写死 0.9。
    threshold: float = 0.83

    # TTL（§22），默认 1 小时
    default_ttl: int = 3600

    # Cacheability Policy：temperature 高于该值不缓存（§36）
    max_temperature: float = 0.7

    embedding_provider: Literal["mock", "qwen"] = "mock"
    store_backend: Literal["memory", "postgres"] = "memory"

    # PostgreSQL + pgvector（§28，可选）
    database_url: str = ""

    # Qwen Embedding（§11，可选，需 DASHSCOPE_API_KEY）
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "text-embedding-v4"

    # 成本换算（§45）：$/1M tokens
    input_cost_per_million: float = 1.0
    output_cost_per_million: float = 5.0

    # Cache Confidence 的安全边际（§40）
    confidence_margin: float = 0.02

    @classmethod
    def from_env(cls) -> "CacheConfig":
        return cls(
            namespace=os.getenv("CACHE_NAMESPACE", "semantic-cache"),
            dim=int(os.getenv("EMBEDDING_DIM", "1024")),
            top_k=int(os.getenv("CACHE_TOP_K", "5")),
            threshold=float(os.getenv("CACHE_THRESHOLD", "0.83")),
            default_ttl=int(os.getenv("CACHE_TTL", "3600")),
            max_temperature=float(os.getenv("CACHE_MAX_TEMPERATURE", "0.7")),
            embedding_provider=os.getenv("EMBEDDING_PROVIDER", "mock"),  # type: ignore[assignment]
            store_backend=os.getenv("CACHE_STORE", "memory"),  # type: ignore[assignment]
            database_url=os.getenv("DATABASE_URL", ""),
            qwen_api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            qwen_base_url=os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            qwen_model=os.getenv("QWEN_EMBEDDING_MODEL", "text-embedding-v4"),
            input_cost_per_million=float(os.getenv("INPUT_COST_PER_MILLION", "1.0")),
            output_cost_per_million=float(os.getenv("OUTPUT_COST_PER_MILLION", "5.0")),
        )
