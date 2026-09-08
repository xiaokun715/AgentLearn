"""agent-memory —— 第十一章 Agent 记忆系统 Demo。

三层结构(核心全部零第三方依赖)：
    records/embedding/engine   —— 8 原语记忆引擎(说明书§核心操作)
    tiers/                      —— 四层记忆架构(说明书§分层记忆)
    worker/distill + decay      —— 离线蒸馏与遗忘代谢

最常用的三个入口：
    from memory_engine import MemoryEngine, MemoryRecord   # 只用引擎 8 原语
    from memory_engine.factory import build_runtime         # 拿整套装配(Runtime)
    from memory_engine.main import app                      # FastAPI 观测服务
"""
from __future__ import annotations

from .config import AgentMemoryConfig
from .embedding import KeywordEmbedder, cosine_similarity
from .engine import MemoryEngine
from .records import EPISODIC, PROFILE, SEMANTIC, MemoryRecord, MemoryType
from .factory import Runtime, build_runtime
from .tiers import (
    AgentMemorySystem,
    EpisodicMemory,
    ProfileStore,
    SemanticMemory,
    ShortTermMemory,
    WorkingMemory,
)
from .worker.distill import DistilledFact, Step
from .worker.decay import DecayWorker

__all__ = [
    # 引擎原语
    "MemoryEngine",
    "MemoryRecord",
    "MemoryType",
    "PROFILE",
    "EPISODIC",
    "SEMANTIC",
    # 向量
    "KeywordEmbedder",
    "cosine_similarity",
    # 四层记忆 + 编排
    "WorkingMemory",
    "ShortTermMemory",
    "EpisodicMemory",
    "SemanticMemory",
    "ProfileStore",
    "AgentMemorySystem",
    # worker
    "DistilledFact",
    "Step",
    "DecayWorker",
    # 装配
    "AgentMemoryConfig",
    "Runtime",
    "build_runtime",
]
