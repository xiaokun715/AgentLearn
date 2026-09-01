"""BaseAgent —— Agent 执行抽象（设计说明书 §16）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..domain.job import Job
from .state import AgentState

if TYPE_CHECKING:
    from .context import StepContext


class BaseAgent(ABC):
    """一个多步骤 Agent。

    - ``name``   注册名，POST /v1/jobs 时通过 ``agent`` 字段指定。
    - ``steps``  有序步骤列表，Executor 按序执行并在每步后 Checkpoint（§20）。
    - ``execute_step`` 执行单个步骤，返回可并入 AgentState 的 dict。
    """

    name: str = ""
    description: str = ""
    steps: list[str] = []
    tools: dict = {}  # name -> Tool

    @abstractmethod
    def initial_state(self, job: Job) -> AgentState:
        """创建初始 AgentState。"""

    @abstractmethod
    async def execute_step(self, step: str, state: AgentState, ctx: "StepContext") -> dict:
        """执行一个 step，返回 {状态字段: 值}。"""
