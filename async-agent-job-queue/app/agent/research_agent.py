"""ResearchAgent（设计说明书 §16）—— 演示标准的多步骤 Agent。

    Step 1: Analyze Query      分析查询
    Step 2: Search             搜索（Tool）
    Step 3: Analyze Result     分析结果
    Step 4: Generate Report    生成报告
"""
from __future__ import annotations

from ..domain.job import Job
from .base import BaseAgent
from .context import StepContext
from .llm import MockLLM
from .state import AgentState
from .tools import SearchWebTool


class ResearchAgent(BaseAgent):
    name = "research_agent"
    description = "分析查询 -> 搜索 -> 分析结果 -> 生成研究报告"
    steps = ["analyze", "search", "analyze_result", "generate_report"]
    tools = {"search_web": SearchWebTool()}

    def __init__(self, llm: MockLLM | None = None) -> None:
        self.llm = llm or MockLLM()

    def initial_state(self, job: Job) -> AgentState:
        return AgentState.initial(job)

    async def execute_step(self, step: str, state: AgentState, ctx: StepContext) -> dict:
        if step == "analyze":
            plan = await ctx.llm(f"研究计划：{state.query}")
            return {"analysis": plan}

        if step == "search":
            results = await ctx.tool("search_web", query=state.query, top_k=3)
            return {"search_result": results}

        if step == "analyze_result":
            snippet = "\n".join(f"- {r}" for r in state.search_result)
            insights = await ctx.llm(
                f"基于以下搜索结果提炼关键洞察：\n{snippet}"
            )
            return {"insights": insights}

        if step == "generate_report":
            report = await ctx.llm(
                f"撰写研究报告：\nquery={state.query}\ninsights={state.insights}"
            )
            return {"report": report}

        raise ValueError(f"unknown step: {step}")
