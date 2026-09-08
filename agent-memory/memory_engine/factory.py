"""Runtime 装配工厂(姊妹 Demo 同一套惯例)：把引擎/四层记忆/worker 一次性搭好。

``build_runtime(config)`` 产出一个 ``Runtime`` dataclass，API、examples、tests 全部共用它；
测试想用空库就传 ``config=AgentMemoryConfig(seed_demo=False)``。
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import AgentMemoryConfig
from .embedding import KeywordEmbedder
from .engine import MemoryEngine
from .tiers import (
    DEFAULT_USER,
    EpisodicMemory,
    ProfileStore,
    SemanticMemory,
    ShortTermMemory,
    WorkingMemory,
)
from .tiers.system import AgentMemorySystem, CANNED_EPISODIC_SEED as _SEED_FACTS
from .worker.decay import DecayWorker

#: 预置画像(说明书：“用户发起请求时直接全量静态加载，不走向量搜索”)
_DEMO_PROFILE = {
    "语言": "中文",
    "集群偏好": "华东2 / 移动网优组",
    "代码习惯": "Python + FastAPI",
    "答复风格": "先给结论再给证据",
}


@dataclass
class Runtime:
    """系统装配完的把手：API / examples / 测试只认这一个对象。"""

    config: AgentMemoryConfig
    engine: MemoryEngine
    profile: ProfileStore
    semantic: SemanticMemory
    working: WorkingMemory
    shortterm: ShortTermMemory
    episodic: EpisodicMemory
    system: AgentMemorySystem
    decay: DecayWorker


def _seed(rt: Runtime) -> None:
    """预置一条演示用的用户画像与若干条排障情景经验，让 Demo 一开始就能召回。"""
    rt.profile.set_many(DEFAULT_USER, _DEMO_PROFILE)

    # 情景经验种子 —— 新任务 query 命中的“上次如何排查”素材
    for subject, predicate, value in _SEED_FACTS:
        rt.episodic.save_fact(
            user_id=DEFAULT_USER,
            subject=subject,
            predicate=predicate,
            object_value=value,
            confidence=0.85,
        )

    # 语义规则种子(点查命中示例)
    rt.semantic.save_rule(
        user_id=DEFAULT_USER,
        subject="时延优化",
        object_value="优先核查传输侧抖动与核心网时延，再查无线空口。",
    )


def build_runtime(config: AgentMemoryConfig | None = None) -> Runtime:
    config = config or AgentMemoryConfig.from_env()

    embedder = KeywordEmbedder(dim=config.dim)
    engine = MemoryEngine(embedder=embedder)
    profile = ProfileStore()
    semantic = SemanticMemory(engine)
    working = WorkingMemory(budget_units=config.working_budget_units)
    shortterm = ShortTermMemory(window=config.shortterm_window)
    episodic = EpisodicMemory(
        engine, top_k=config.recall_top_k, sim_threshold=config.sim_threshold
    )
    system = AgentMemorySystem(
        engine=engine,
        working=working,
        shortterm=shortterm,
        episodic=episodic,
        profile=profile,
        semantic=semantic,
        config=config,
    )
    decay = DecayWorker(engine, config)

    rt = Runtime(
        config=config,
        engine=engine,
        profile=profile,
        semantic=semantic,
        working=working,
        shortterm=shortterm,
        episodic=episodic,
        system=system,
        decay=decay,
    )
    if config.seed_demo:
        _seed(rt)
    return rt
