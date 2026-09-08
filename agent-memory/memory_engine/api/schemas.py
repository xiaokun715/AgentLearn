"""观测 API 的请求/响应模型(唯一允许引入 pydantic 的地方，核心逻辑保持零依赖)。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

MemoryTypeIn = Literal["PROFILE", "EPISODIC", "SEMANTIC"]


class MemoryCreate(BaseModel):
    user_id: str = Field(default="alice", description="归属用户")
    subject: str
    predicate: str = "事实"
    object_value: str
    memory_type: MemoryTypeIn = "SEMANTIC"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class SupersedeIn(BaseModel):
    """SCD2 换代入参：旧记忆软失效，派生一条 version+1 的新记忆。"""

    new_value: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class TouchIn(BaseModel):
    succeeded: bool = Field(default=True, description="被证伪时传 false，会降权直至失活")


class RecallIn(BaseModel):
    user_id: str = "alice"
    query: str
    top_k: int | None = Field(default=None, ge=1, le=20)
    sim_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class ConsolidateIn(BaseModel):
    memory_ids: list[str]
    generalized_value: str

    @model_validator(mode="after")
    def _non_empty(self) -> "ConsolidateIn":
        if not self.memory_ids:
            raise ValueError("memory_ids 不能为空")
        return self


class DecayRunOnceIn(BaseModel):
    simulate_idle_days: float = Field(
        default=0.0,
        ge=0.0,
        description="先让整个世界快进这么多天(演示艾宾浩斯衰减)，再执行代谢",
    )
    half_life_days: float | None = None
    retention_floor: float | None = None


class SessionObserveIn(BaseModel):
    user_id: str = "alice"
    query: str
    system_prompt: str = ""


class SessionStep(BaseModel):
    action: str
    ok: bool = True
    detail: str = ""


class SessionCommitIn(BaseModel):
    user_id: str = "alice"
    goal: str
    steps: list[SessionStep]
    outcome: bool = True
    also_reflect: bool = True


class IdPath(BaseModel):
    memory_id: str
