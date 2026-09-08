"""可调参数集中地：所有“阈值/预算/周期”都能从一处改，方便 demo 调小、测试加快。

对齐姊妹 Demo 的惯例：dataclass 配置 + ``from_env()``(AGENTMEMORY_* 前缀)，
不引 pydantic-settings，保持核心零依赖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class AgentMemoryConfig:
    """记忆系统全部旋钮。

    * ``sim_threshold`` / ``recall_top_k`` —— 检索质量(本 Demo 用轻量词袋嵌入，默认 0.30 即可召回；
      引擎 ``MemoryEngine.retrieve`` 的方法级默认 0.70 是说明书原值，供“真实嵌入”使用)。
    * ``half_life_days`` / ``retention_floor`` —— 艾宾浩斯衰减的半衰期与沉睡红线(说明书§代谢)。
    * ``working_budget_units`` —— 工作记忆 Token 预算，超预算丢最旧帧(说明书§严格入模过滤)。
    * ``shortterm_window`` —— 短期记忆超过 N 轮即把早期轮折叠成摘要(说明书§动态折叠)。
    * ``decay_interval_s`` —— 后台 DecayWorker 周期；demo/测试可直接调 ``run_once()``。
    """

    # 嵌入与检索
    dim: int = field(default_factory=lambda: _env_int("AGENTMEMORY_DIM", 256))
    recall_top_k: int = field(default_factory=lambda: _env_int("AGENTMEMORY_TOP_K", 3))
    sim_threshold: float = field(
        default_factory=lambda: _env_float("AGENTMEMORY_SIM_THRESHOLD", 0.30)
    )

    # 遗忘代谢
    half_life_days: float = field(
        default_factory=lambda: _env_float("AGENTMEMORY_HALF_LIFE_DAYS", 30.0)
    )
    retention_floor: float = field(
        default_factory=lambda: _env_float("AGENTMEMORY_RETENTION_FLOOR", 0.20)
    )

    # 分层记忆预算
    working_budget_units: int = field(
        default_factory=lambda: _env_int("AGENTMEMORY_WORKING_BUDGET", 600)
    )
    shortterm_window: int = field(
        default_factory=lambda: _env_int("AGENTMEMORY_SHORTTERM_WINDOW", 6)
    )

    # 后台任务
    decay_interval_s: float = field(
        default_factory=lambda: _env_float("AGENTMEMORY_DECAY_INTERVAL_S", 60.0)
    )

    # 工厂装配时是否预置演示种子数据(1 份画像 + 若干条 EPISODIC 排障经验)
    seed_demo: bool = True

    @classmethod
    def from_env(cls) -> "AgentMemoryConfig":
        return cls()

    def as_dict(self) -> dict:
        """观测 API 用：把当前参数平铺输出。"""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_")
        }
