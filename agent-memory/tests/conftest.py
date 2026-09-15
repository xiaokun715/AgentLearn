"""pytest 共享夹具：空引擎 / 带种子 Runtime / 观测 API 客户端。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from memory_engine.config import AgentMemoryConfig
from memory_engine.embedding import KeywordEmbedder
from memory_engine.engine import MemoryEngine
from memory_engine.factory import Runtime, build_runtime
from memory_engine.main import create_app

DIM = 128  # 测试用小维度, 更快


@pytest.fixture()
def engine() -> MemoryEngine:
    """空白记忆引擎(无种子)。"""
    return MemoryEngine(embedder=KeywordEmbedder(dim=DIM))


@pytest.fixture()
def rt_clean() -> Runtime:
    """无种子的完整装配(Runtime)。"""
    return build_runtime(AgentMemoryConfig(seed_demo=False))


@pytest.fixture()
def rt_seed() -> Runtime:
    """带演示种子(画像 + 4 条排障经验 + 1 条语义规则)。"""
    return build_runtime(AgentMemoryConfig(seed_demo=True))


@pytest.fixture()
def client() -> TestClient:
    """观测 API 客户端(带种子数据, lifespan 会启动 60s 周期的衰减 worker)。"""
    rt = build_runtime(AgentMemoryConfig(seed_demo=True))
    app = create_app(runtime=rt)
    with TestClient(app) as c:
        yield c
