"""AgentRegistry —— agent_name -> BaseAgent 的注册表。"""
from __future__ import annotations

from ..domain.job import Job
from .base import BaseAgent
from .chaos_agent import ChaosAgent
from .research_agent import ResearchAgent
from .state import AgentState


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register(ResearchAgent())
        self.register(ChaosAgent())

    def register(self, agent: BaseAgent) -> None:
        if not agent.name:
            raise ValueError("agent.name must be set")
        self._agents[agent.name] = agent

    def get(self, name: str) -> BaseAgent:
        if name not in self._agents:
            raise KeyError(f"unknown agent: {name!r}. available={sorted(self._agents)}")
        return self._agents[name]

    def names(self) -> list[str]:
        return sorted(self._agents)

    def build_initial_state(self, agent_name: str, job: Job) -> AgentState:
        return self.get(agent_name).initial_state(job)
