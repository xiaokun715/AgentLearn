"""TaskSupervisor —— 第 4 道防线：状态回滚 + 优雅降级（Partial Yield）。

它包在 Agent 协程外面，统一接住三种结局：

1. 正常跑完            -> status=COMPLETED，透出 result；
2. ``AgentStopRequested``（层2 协作式在动作边界跳出）-> 捕获后走 ``_shutdown``；
3. ``asyncio.CancelledError``（层3 掐断了 I/O）      -> 捕获后走 ``_shutdown``；
4. 其它异常            -> status=FAILED，同样先善后清理，避免“带着脏数据死掉”。

不管以哪种方式中止，``_shutdown`` 都保证：
- 回滚未提交的临时表（SimLedger.rollback，即“事务回滚”）；
- 删除残留的临时文件（ReportWorkspace.cleanup）；
- 基于已收集事实生成一段友好的“部分产出”，而不是报一个冰冷的红字错误。
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..domain.events import TaskEventType
from ..domain.task import AgentTask, TaskStatus
from .artifacts import build_partial
from .base import AgentContext, AgentStopRequested

logger = logging.getLogger(__name__)


class TaskSupervisor:
    def __init__(self, task: AgentTask, ctx: AgentContext) -> None:
        self.task = task
        self.ctx = ctx

    async def supervise(self, run_coro):
        self.ctx.emit(TaskEventType.TASK_STARTED, {"agent": self.task.agent_name})
        try:
            result = await run_coro
        except AgentStopRequested as e:      # 层2：动作边界协作式跳出
            await self._shutdown("cooperative", reason=str(e))
            return
        except asyncio.CancelledError:        # 层3：asyncio 掐断底层 I/O
            await self._shutdown("force", reason="asyncio.Task.cancel() 掐断底层 I/O")
            return
        except Exception as e:                # 其它异常：至少也要善后，不能带脏数据死掉
            logger.exception("task %s failed", self.task.task_id)
            await self._fail(str(e))
            return
        else:
            self.task.status = TaskStatus.COMPLETED
            self.task.result = result
            self.task.finished_at = time.time()
            self.ctx.emit(TaskEventType.TASK_COMPLETED, {"result": result})

    # ---- 善后（层4） -----------------------------------------------------------
    async def shutdown_early(self) -> None:
        """取消发生在 Agent 协程真正开始跑之前的最外层兜底。"""
        await self._shutdown("force", reason="任务在启动前即被取消（asyncio cancel）")

    async def _shutdown(self, cause: str, *, reason: str) -> None:
        """取消统一收尾：回滚 + 清理 + Partial Yield。"""
        if cause == "cooperative":
            self.ctx.emit(TaskEventType.LOOP_CANCEL_BREAK, {"reason": reason})
        else:  # force
            self.ctx.emit(TaskEventType.FORCE_CANCELLED, {"reason": reason})

        self.ctx.emit(TaskEventType.CLEANUP_STARTED, {"cause": cause})
        stats = self.ctx.cleanup_snapshot()

        self.task.status = TaskStatus.CANCELLED
        self.task.cancel_requested = True
        self.task.finished_at = time.time()

        partial, meta = build_partial(self.task, cause, stats)
        self.task.partial_yield = partial
        self.task.partial_meta = meta
        self.ctx.emit(TaskEventType.PARTIAL_YIELD, {"text": partial})
        self.ctx.emit(TaskEventType.TASK_CANCELLED, {
            "cause": cause, "reason": reason,
            "facts_count": len(self.task.facts),
            "partial": partial,
        })

    async def _fail(self, error: str) -> None:
        """失败：做同样的事后清理，但不产出 Partial Yield（没有可信任的部分结果）。"""
        self.ctx.emit(TaskEventType.CLEANUP_STARTED, {"cause": "error"})
        self.ctx.cleanup_snapshot()
        self.task.status = TaskStatus.FAILED
        self.task.error = error
        self.task.finished_at = time.time()
        self.ctx.emit(TaskEventType.TASK_FAILED, {"error": error})
