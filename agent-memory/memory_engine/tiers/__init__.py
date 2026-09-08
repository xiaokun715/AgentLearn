"""四层记忆架构(说明书 §经典四层记忆金字塔模型)。

    Working(工作记忆)     —— working.WorkingMemory    : 有预算的上下文帧
    Short-Term(短期)      —— shortterm.ShortTermMemory : 会话轮次+折叠摘要+Checkpointer
    Episodic(情景)        —— episodic.EpisodicMemory   : 自传式经验 Top-K RAG
    Semantic+Profile(语义) —— semantic.{SemanticMemory,ProfileStore}
    system.AgentMemorySystem —— 串起各层的编排器
"""
from __future__ import annotations

from .episodic import EpisodicMemory
from .semantic import ProfileStore, SemanticMemory
from .shortterm import ShortTermMemory
from .system import (
    AgentMemorySystem,
    CANNED_5G,
    CANNED_EPISODIC_SEED,
    CannedScenario,
    DEFAULT_USER,
)
from .working import Frame, WorkingMemory

__all__ = [
    "WorkingMemory",
    "Frame",
    "ShortTermMemory",
    "EpisodicMemory",
    "ProfileStore",
    "SemanticMemory",
    "AgentMemorySystem",
    "CannedScenario",
    "CANNED_5G",
    "CANNED_EPISODIC_SEED",
    "DEFAULT_USER",
]
