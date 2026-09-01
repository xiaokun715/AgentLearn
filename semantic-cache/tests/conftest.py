"""共享测试 fixtures（设计说明书 §47 tests/）。"""
from __future__ import annotations

import pytest

from semantic_cache.core.cache import SemanticCache
from semantic_cache.core.entry import ChatRequest, Message
from semantic_cache.core.policy import CachePolicy, ThresholdPolicy
from semantic_cache.embedding.mock import MockEmbeddingGenerator
from semantic_cache.metrics.metrics import CacheMetrics
from semantic_cache.normalize.normalizer import PromptNormalizer
from semantic_cache.safety.validator import SafetyValidator
from semantic_cache.storage.memory import MemoryStore

# Mock 向量标定出的阈值（见 tests/test_similarity.py / README）：同义 ~0.90+，相关但不同 ~0.78 以下
DEFAULT_THRESHOLD = 0.83


@pytest.fixture
def normalizer() -> PromptNormalizer:
    return PromptNormalizer()


@pytest.fixture
def embedding() -> MockEmbeddingGenerator:
    return MockEmbeddingGenerator()


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def metrics() -> CacheMetrics:
    return CacheMetrics()


@pytest.fixture
def cache(store, embedding, metrics) -> SemanticCache:
    # 与 build_cache() 保持一致：惰性删除过期条目时计数（§41 cache_expiration_total）
    store._on_expired = metrics.record_expiration
    return SemanticCache(
        store=store,
        normalizer=PromptNormalizer(),
        embedding=embedding,
        threshold_policy=ThresholdPolicy(threshold=DEFAULT_THRESHOLD),
        cache_policy=CachePolicy(max_temperature=0.7),
        validator=SafetyValidator(),
        metrics=metrics,
        default_ttl=3600,
        top_k=5,
    )


def _make_request(
    text: str,
    *,
    model: str = "qwen",
    tenant: str = "tenant-A",
    system: str | None = None,
    temperature: float = 0.0,
    **kwargs,
) -> ChatRequest:
    messages = []
    if system:
        messages.append(Message(role="system", content=system))
    messages.append(Message(role="user", content=text))
    return ChatRequest(
        user_id="user-001",
        model=model,
        messages=messages,
        tenant_id=tenant,
        temperature=temperature,
        **kwargs,
    )


def _make_response(text: str = "answer", input_tokens: int = 120, output_tokens: int = 80) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "qwen",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


@pytest.fixture
def make_request():
    """构造 ChatRequest 的工厂（默认 tenant-A / model=qwen）。"""
    return _make_request


@pytest.fixture
def make_response():
    """构造 OpenAI 风格响应的工厂（含 usage，供 Token Saving 计算）。"""
    return _make_response
