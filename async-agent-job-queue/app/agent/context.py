"""StepContext —— 单个 step 执行时暴露给 Agent 的能力（设计说明书 §21-23）。

核心：``ctx.tool()`` 内建 Tool 幂等（Tool Execution Record + write-ahead）。
- 幂等命中：直接返回已保存的 result，绝不重复执行（§23）。
- 否则：先写 running 记录到 Checkpoint（write-ahead），再真正执行；
  执行成功后把 status 更新为 success 并落盘（§40 处理「Tool 成功但
  Checkpoint 没保存」的窗口）。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import JobEventType
from .tools import make_tool_call_id

if TYPE_CHECKING:
    from ..domain.job import Job
    from .base import BaseAgent
    from .state import AgentState

logger = logging.getLogger(__name__)


class StepContext:
    def __init__(
        self,
        *,
        executor,
        agent: "BaseAgent",
        job: "Job",
        state: "AgentState",
        gate=None,
    ) -> None:
        self._executor = executor
        self._agent = agent
        self.job = job
        self._state = state
        self._gate = gate
        self.step: str | None = None

    # ---- 生命周期 -----------------------------------------------------------

    def check_cancel(self) -> None:
        """检查取消 / 租约丢失信号；必要时抛出异常（§10/§44）。"""
        if self._gate is not None:
            self._gate.raise_if_aborted()

    # ---- LLM ---------------------------------------------------------------

    async def llm(self, prompt: str, **kwargs) -> str:
        self.check_cancel()
        await self._executor.emit_event(
            self.job.id, JobEventType.LLM_CALLED.value, {"step": self.step}
        )
        try:
            text = await self._executor.llm.generate(prompt, **kwargs)
        except Exception as e:
            await self._executor.emit_event(
                self.job.id, JobEventType.LLM_FAILED.value,
                {"step": self.step, "error": str(e)},
            )
            raise
        await self._executor.emit_event(
            self.job.id, JobEventType.LLM_COMPLETED.value,
            {"step": self.step, "length": len(text)},
        )
        return text

    # ---- Tool（幂等执行） -----------------------------------------------------

    def is_tool_cached(self, tool_name: str, **kwargs) -> bool:
        """判断某次 Tool 调用是否已有成功的执行记录（恢复时用于避免重复执行）。"""
        rec = self._state.tool_records.get(make_tool_call_id(tool_name, kwargs))
        return rec is not None and rec.get("status") == "success"

    async def tool(self, tool_name: str, **kwargs) -> dict:
        self.check_cancel()
        tool = self._agent.tools[tool_name]
        call_id = make_tool_call_id(tool_name, kwargs)

        # 1) 幂等命中：已有 SUCCESS 记录 -> 直接返回，不重复执行（§23）
        rec = self._state.tool_records.get(call_id)
        if rec is not None and rec.get("status") == "success":
            await self._executor.emit_event(
                self.job.id, JobEventType.TOOL_SKIPPED.value,
                {"step": self.step, "tool": tool_name, "tool_call_id": call_id},
            )
            logger.info("job %s tool %s idempotent hit, skipped", self.job.id, call_id)
            return rec["result"]

        # 2) 上一轮崩溃可能留下 running 记录：视为未完成，重新执行
        #    （只读 Tool 天然幂等；非幂等 Tool 需要外部幂等键，见 README §22）
        self._state.tool_records[call_id] = {
            "tool_call_id": call_id, "tool": tool_name, "arguments": kwargs, "status": "running",
        }
        # 3) write-ahead：先把 running 记录落盘，再执行（§40）
        await self._executor.save_checkpoint(self._state, reason="tool_start")
        await self._executor.emit_event(
            self.job.id, JobEventType.TOOL_CALLED.value,
            {"step": self.step, "tool": tool_name, "tool_call_id": call_id},
        )

        try:
            result = await tool.run(**kwargs)
        except Exception:
            await self._executor.emit_event(
                self.job.id, JobEventType.STEP_FAILED.value,
                {"step": self.step, "tool": tool_name, "tool_call_id": call_id},
            )
            raise

        # 4) 更新为 success 并落盘 —— Tool Execution Record（§23）
        self._state.tool_records[call_id] = {
            "tool_call_id": call_id, "tool": tool_name, "arguments": kwargs,
            "status": "success", "result": result,
        }
        await self._executor.save_checkpoint(self._state, reason="tool_end")
        await self._executor.emit_event(
            self.job.id, JobEventType.TOOL_COMPLETED.value,
            {"step": self.step, "tool": tool_name, "tool_call_id": call_id},
        )
        return result
