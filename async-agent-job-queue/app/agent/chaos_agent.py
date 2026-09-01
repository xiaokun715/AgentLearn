"""ChaosAgent —— 故障注入 Agent（设计说明书 §41）。

Demo 一定要做故障注入，否则只是写了一个普通 Queue。
通过 job.input["chaos"] 控制行为：

    {
      "query": "分析 NVIDIA 最新财报",
      "chaos": {
        "fail_at":         ["search"],              # 在哪些 step 上失败
        "fail_attempts":   {"search": 2},           # 各 step 连续失败几次后成功
        "fail_with":       "retryable",             # retryable | non_retryable | crash
        "crash_after_tool": true,                   # tool 成功后、step checkpoint 前崩溃
        "llm_latency": 0.03,
        "tool_latency": 0.03
      }
    }

- ``retryable``         -> 触发 RetryPolicy 重试（§24）
- ``non_retryable``     -> 直接 FAILED
- ``crash`` / ``crash_after_tool`` -> 模拟 Worker 崩溃，交给 Lease + Reaper 恢复（§41）
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..domain.exceptions import NonRetryableError, RetryableError, WorkerCrash
from ..domain.job import Job
from .base import BaseAgent
from .context import StepContext
from .llm import MockLLM
from .state import AgentState
from .tools import SearchWebTool


class ChaosAgent(BaseAgent):
    name = "chaos_agent"
    description = "故障注入 Agent —— 演示 Retry / 崩溃恢复 / 幂等"
    steps = ["analyze", "search", "analyze_result", "generate_report"]
    tools = {"search_web": SearchWebTool()}

    def __init__(self, llm: MockLLM | None = None) -> None:
        self.llm = llm or MockLLM()

    def initial_state(self, job: Job) -> AgentState:
        return AgentState.initial(job)

    async def execute_step(self, step: str, state: AgentState, ctx: StepContext) -> dict:
        cfg = ctx.job.input.get("chaos", {}) or {}

        # ---- 故障注入：按 retry_count 决定该 step 是否失败 --------------------
        fail_at = cfg.get("fail_at", []) or []
        fail_attempts = cfg.get("fail_attempts", {}) or {}
        if step in fail_at:
            allowed_failures = int(fail_attempts.get(step, 1))
            if ctx.job.retry_count < allowed_failures:
                await asyncio.sleep(float(cfg.get("failure_latency", 0.02)))
                raise _build_failure(cfg.get("fail_with", "retryable"), step)

        # ---- 正常执行 --------------------------------------------------------
        if step == "analyze":
            return {"analysis": await ctx.llm(f"研究计划：{state.query}")}

        if step == "search":
            cached = ctx.is_tool_cached("search_web", query=state.query, top_k=3)
            results = await ctx.tool("search_web", query=state.query, top_k=3)
            # 崩溃注入：tool 成功、step checkpoint 前崩溃（§40 的窗口）
            if cfg.get("crash_after_tool") and not cached:
                raise WorkerCrash("simulated worker crash after tool execution")
            return {"search_result": results}

        if step == "analyze_result":
            return {"insights": await ctx.llm(f"洞察：{state.query}")}

        if step == "generate_report":
            return {"report": await ctx.llm(f"报告：{state.query}")}

        raise ValueError(f"unknown step: {step}")


def _build_failure(kind: str, step: str) -> Exception:
    if kind == "non_retryable":
        return NonRetryableError(f"invalid prompt at step {step}")
    if kind == "crash":
        return WorkerCrash(f"simulated crash at step {step}")
    return RetryableError(f"LLM timeout at step {step}")
