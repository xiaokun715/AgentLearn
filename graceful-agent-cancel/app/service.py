"""TaskService —— 把一次“Agent 飞奔任务”和它的取消链路串起来。

职责：
  submit()  -> 异步提交：建记录 + 发事件 + 立刻 spawn 一个 asyncio.Task 去跑 Agent
              （绝不阻塞 HTTP 请求等 Agent 跑完，交互层“立刻拿到 task_id”）
  cancel()  -> 收到用户 Stop（层1）：记录 CANCELLATION_REQUESTED，
               然后按 mode 交给 RunningTaskRegistry 执行层2/层3 的取消
  get()     -> 任务快照（含 Partial Yield / 结果）
"""
from __future__ import annotations

import asyncio
import logging

from .agent.base import AgentContext
from .agent.research_agent import ResearchAgent
from .agent.supervisor import TaskSupervisor
from .cancellation.registry import RunningTaskRegistry
from .config import AgentConfig
from .domain.events import EventBus, TaskEventType
from .domain.exceptions import TaskNotFoundError
from .domain.task import AgentTask, TaskStatus
from .store.base import CancellationStore

logger = logging.getLogger(__name__)

AGENT_REGISTRY = {"research_agent": ResearchAgent}


class TaskService:
    def __init__(
        self,
        *,
        config: AgentConfig,
        bus: EventBus,
        cancel_store: CancellationStore,
        registry: RunningTaskRegistry,
    ) -> None:
        self.config = config
        self.bus = bus
        self.cancel_store = cancel_store
        self.registry = registry
        self._tasks: dict[str, AgentTask] = {}

    # ---- 提交（交互层：立即返回 task_id） -------------------------------------
    def submit(self, *, query: str, city: str = "上海", days: int = 2,
               agent: str = "research_agent") -> AgentTask:
        if agent not in AGENT_REGISTRY:
            from .domain.exceptions import UnknownAgentError
            raise UnknownAgentError(f"unknown agent: {agent}")

        task = AgentTask(query=query, city=city, days=days, agent_name=agent)
        self._tasks[task.task_id] = task
        self.bus.emit(task.task_id, TaskEventType.TASK_CREATED, {"query": query, "agent": agent})

        # 每个请求 -> 一个可取消的 asyncio.Task（层3 取消的目标就是这个）
        coro = self._drive(task)
        handle = asyncio.get_running_loop().create_task(coro, name=f"agent:{task.task_id}")
        self.registry.register(task.task_id, handle)
        handle.add_done_callback(lambda _t: self.registry.unregister(task.task_id))
        return task

    async def _drive(self, task: AgentTask) -> None:
        self.registry.mark_started(task.task_id)   # 第一行：标记“协程已真正开始”
        agent = AGENT_REGISTRY[task.agent_name]()
        ctx = AgentContext(task=task, bus=self.bus,
                           store=self.cancel_store, config=self.config)
        supervisor = TaskSupervisor(task, ctx)
        try:
            await supervisor.supervise(agent.run(task, ctx))
        except asyncio.CancelledError:
            # 取消发生在 asyncio 任务“启动前”（提交后立刻 Stop）：supervisor 内部的
            # except 来不及接住，这里做最外层兜底，仍要保证善后 + Partial Yield + 终态。
            await supervisor.shutdown_early()

    # ---- 取消入口（层1 -> 层2/层3）-------------------------------------------
    def cancel(self, task_id: str, mode: str | None = None) -> dict:
        from .cancellation.registry import CANCEL_MODES
        task = self.get(task_id)
        mode = mode or self.config.default_cancel_mode
        if mode not in CANCEL_MODES:
            raise ValueError(f"mode 必须是 {CANCEL_MODES} 之一，got {mode!r}")

        if task.status.is_terminal:
            return {
                "task_id": task_id, "status": task.status.value,
                "cancel_requested": task.cancel_requested, "mode": mode,
                "note": "任务已到达终态，无需再取消",
            }

        # 网关记下这次停止指令（前端能立刻在 SSE 里看到“已请求取消”）
        self.bus.emit(task_id, TaskEventType.CANCELLATION_REQUESTED, {"mode": mode})

        # 层2：标记 cancelled（Agent 下一个动作边界会撞上跳出）
        # 层3：若 force，同时向正在执行的 asyncio.Task 抛取消信号，掐断 I/O
        action = self.registry.request_cancel(task_id, mode)
        task.cancel_requested = True

        # 特殊边界：force 取消落在“Agent 协程还没开始跑”之前时，
        # asyncio 会直接让任务以 cancelled 结束、**根本不执行协程体**，
        # 于是 supervisor 无从介入 —— 在这里直接替它完成善后 + Partial Yield。
        if action.get("pre_start") and task.status is TaskStatus.RUNNING:
            self._finalize_prestart(task, mode)

        notes = {
            "cooperative": "已标记 cancelled：Agent 将在下一个动作边界（下次调 LLM/工具前）主动跳出循环",
            "force": "已标记 cancelled 并向正在执行的协程抛出取消信号（asyncio.Task.cancel），正在进行的 I/O 会被当场掐断",
        }
        return {
            "task_id": task_id, "status": task.status.value,
            "cancel_requested": True, "mode": mode,
            "action": action, "note": notes.get(mode, ""),
        }

    # ---- pre-start 取消收尾 -----------------------------------------------------
    def _finalize_prestart(self, task: AgentTask, mode: str) -> None:
        """任务协程还没开始就被取消：模拟 supervisor 的 _shutdown 直接收尾。"""
        from .agent.artifacts import build_partial
        import time as _time

        tid = task.task_id
        self.bus.emit(tid, TaskEventType.FORCE_CANCELLED,
                      {"reason": "任务在启动前即被取消（asyncio cancel）"})
        self.bus.emit(tid, TaskEventType.CLEANUP_STARTED, {"cause": "force"})
        stats = {"db_rows_rolled_back": 0, "temp_files_removed": []}
        task.status = TaskStatus.CANCELLED
        task.cancel_requested = True
        task.finished_at = _time.time()
        partial, meta = build_partial(task, "force", stats)
        task.partial_yield = partial
        task.partial_meta = meta
        self.bus.emit(tid, TaskEventType.PARTIAL_YIELD, {"text": partial})
        self.bus.emit(tid, TaskEventType.TASK_CANCELLED, {
            "cause": "force", "reason": "任务在启动前即被取消",
            "facts_count": 0, "partial": partial,
        })

    # ---- 查询 ----------------------------------------------------------------
    def get(self, task_id: str) -> AgentTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return task

    def history(self, task_id: str) -> list[dict]:
        self.get(task_id)
        return [e.to_dict() for e in self.bus.history(task_id)]

    def snapshot(self, task_id: str) -> dict:
        task = self.get(task_id)
        pub = task.to_public()
        pub["events"] = self.history(task_id)
        return pub
