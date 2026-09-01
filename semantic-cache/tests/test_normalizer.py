"""Prompt Normalizer 测试（设计说明书 §6 ~ §9）。"""
from __future__ import annotations

from semantic_cache.core.entry import ChatRequest, Message
from semantic_cache.normalize.normalizer import PromptNormalizer


def make(text, *, model="qwen", system=None, temperature=0.0, **kw):
    messages = []
    if system:
        messages.append(Message(role="system", content=system))
    messages.append(Message(role="user", content=text))
    return ChatRequest(user_id="u1", model=model, messages=messages, temperature=temperature, **kw)


def test_whitespace_normalization(normalizer):
    assert normalizer.normalize_text("  什么是   TCP？  ") == "什么是 tcp?"
    assert normalizer.normalize_text("a\tb\nc") == "a b c"


def test_case_and_fullwidth_normalization(normalizer):
    assert normalizer.normalize_text("What is TCP?") == "what is tcp?"
    assert normalizer.normalize_text("什么是TCP？") == "什么是tcp?"  # 全角 ？ -> 半角 ?


def test_system_prompt_not_in_user_text(normalizer):
    req = make("什么是TCP？", system="你是一个AI助手")
    normalized = normalizer.normalize_request(req)
    assert "AI助手" not in normalized.user_text
    assert normalized.user_text == "什么是tcp?"


def test_fingerprint_deterministic(normalizer):
    a = normalizer.normalize_request(make("什么是TCP？"))
    b = normalizer.normalize_request(make("什么是TCP？"))
    assert a.fingerprint == b.fingerprint


def test_fingerprint_sensitive_to_system_prompt(normalizer):
    a = normalizer.normalize_request(make("什么是TCP？", system="系统A"))
    b = normalizer.normalize_request(make("什么是TCP？", system="系统B"))
    assert a.fingerprint != b.fingerprint


def test_fingerprint_sensitive_to_model(normalizer):
    a = normalizer.normalize_request(make("什么是TCP？", model="qwen"))
    b = normalizer.normalize_request(make("什么是TCP？", model="gpt"))
    assert a.fingerprint != b.fingerprint


def test_fingerprint_sensitive_to_temperature(normalizer):
    a = normalizer.normalize_request(make("什么是TCP？", temperature=0.0))
    b = normalizer.normalize_request(make("什么是TCP？", temperature=0.9))
    assert a.fingerprint != b.fingerprint


def test_fingerprint_sensitive_to_knowledge_version(normalizer):
    a = normalizer.normalize_request(make("什么是TCP？", knowledge_version="v42"))
    b = normalizer.normalize_request(make("什么是TCP？", knowledge_version="v43"))
    assert a.fingerprint != b.fingerprint


def test_system_fingerprint_ignores_user_content(normalizer):
    a = normalizer.normalize_request(make("什么是TCP？", system="系统A"))
    b = normalizer.normalize_request(make("什么是UDP？", system="系统A"))
    assert a.system_fingerprint == b.system_fingerprint
    assert a.fingerprint != b.fingerprint
