"""Agent 循环骨架 —— 对应第十章 2.2 节的 “Agent Loop（while 循环）”。

关键代码就是文档里那段伪代码的落地：

    while True:
        # 1. 每次循环前，查一下用户是不是点取消了
        if redis.get(f"task_status:{task_id}") == "cancelled":
            break  # 立刻跳出死循环
        # 2. 调大模型思考
        # 3. 调外部工具

我们把“查取消”下沉到 :class:`AgentContext`：

- ``ctx.check_cancel()`` 查询 CancellationStore（Demo 里是 dict，生产是 Redis）；
- 它在**每个动作（LLM / 工具）开始前**、以及**每轮 step 开始前**都被插桩调用一次；
- 一旦发现 ``cancelled``，抛出 :class:`AgentStopRequested`，由 supervisor 捕获做兜底。

注意它的边界：它只在“两次动作之间”生效。Agent 正卡在一个 20 秒网络 I/O 内部时，
不会立刻停下 —— 那正是层 3（asyncio.Task.cancel()）要解决的事。
"""
from __future__ import annotations

from ..domain.events import EventBus, TaskEventType
from ..resources.db import SimLedger
from ..resources.workspace import ReportWorkspace


class AgentStopRequested(Exception):
    """层2 协作式取消：Agent 在动作边界撞上 cancelled 标记，主动跳出循环。"""


class AgentContext:
    """单个 task 执行期间暴露给 Agent 的能力 + 取消埋点 + 事件出口。"""

    def __init__(self, *, task, bus: EventBus, store, config) -> None:
        self.task = task
        self.bus = bus
        self._store = store
        self.config = config
        self.agent = None
        self.db: SimLedger | None = None
        self.ws: ReportWorkspace | None = None

    # ---- 装配 ---------------------------------------------------------------
    def attach_agent(self, agent) -> None:
        self.agent = agent

    def attach_resources(self, db: SimLedger, ws: ReportWorkspace) -> None:
        self.db = db
        self.ws = ws

    # ---- 事件出口 ------------------------------------------------------------
    def emit(self, event_type: TaskEventType, payload: dict | None = None):
        return self.bus.emit(self.task.task_id, event_type, payload)

    # ---- 循环层埋点（本章核心）------------------------------------------------
    async def check_cancel(self) -> None:
        """每次核心动作前调用。状态是 cancelled 就主动跳出（层2）。"""
        if self._store.is_cancelled(self.task.task_id):
            raise AgentStopRequested(
                "用户已要求中止：在动作边界撞上 cancelled 标记"
            )

    # ---- 事实收集 ------------------------------------------------------------
    def collect(self, kind: str, text: str) -> None:
        self.task.collect(kind, text)
        self.emit(TaskEventType.FACT_COLLECTED, {"kind": kind, "text": text})

    # ---- LLM / 工具（都自带动作前埋点）----------------------------------------
    async def llm(self, prompt: str) -> str:
        await self.check_cancel()                      # 每次调大模型前插桩
        self.emit(TaskEventType.LLM_THINK_STARTED, {"prompt": prompt[:40]})
        text = await self.agent.llm.think(prompt, ctx=self)
        self.emit(TaskEventType.LLM_THINK_COMPLETED, {"chars": len(text)})
        return text

    async def tool(self, name: str, **kwargs) -> dict:
        await self.check_cancel()                      # 每次执行耗时工具前插桩
        tool = self.agent.tools[name]
        self.emit(TaskEventType.TOOL_STARTED, {"tool": name, "args": kwargs})
        result = await tool.run(ctx=self, **kwargs)
        summary = result.get("summary") or (result.get("snippet", "") if result else "")
        self.emit(TaskEventType.TOOL_COMPLETED, {"tool": name, "summary": str(summary)[:80]})
        return result

    # ---- 兜底善后（层4）：事务回滚 + 临时文件清理 ------------------------------
    def cleanup_snapshot(self) -> dict:
        """取消/失败后统一善后。返回清理统计，供 Partial Yield 拼话术。"""
        db_rows = self.db.rollback() if self.db is not None else 0
        if db_rows > 0:
            self.emit(TaskEventType.DB_ROLLED_BACK, {"rows_dropped": db_rows})

        ws_info = self.ws.cleanup() if self.ws is not None else None
        if ws_info is not None:
            self.emit(TaskEventType.TEMP_CLEANED, ws_info)

        return {
            "db_rows_rolled_back": db_rows,
            "temp_files_removed": (ws_info or {}).get("removed_files", []),
            "transaction_open": self.db.in_transaction if self.db is not None else False,
        }


class BaseAgent:
    """可取消的 Agent 循环模板。

    子类只需声明 ``steps``（顺序执行的动作名）并实现 ``_step_<name>``。
    ``run`` 在每一步前插桩一次 ``check_cancel``（层2）；每个 LLM/工具动作在
    ``AgentContext.llm/tool`` 内部还会再插桩一次。
    """

    name = "base"
    description = ""
    steps: list[str] = []
    tools: dict = {}

    def plan(self, task) -> list[str]:
        return list(self.steps)

    async def run(self, task, ctx: AgentContext) -> dict | None:
        # 一开始就建好“脏数据源”，保证任意时刻取消都有可回滚/可清理的东西
        db = SimLedger()
        ws = ReportWorkspace(task.task_id)
        ctx.attach_agent(self)
        ctx.attach_resources(db, ws)
        ws.create()

        task.total_steps = len(self.plan(task))
        result: dict | None = None
        for i, step in enumerate(self.plan(task)):
            await ctx.check_cancel()                     # 层2：每轮循环前埋点
            task.current_step = step
            task.step_index = i
            ctx.emit(TaskEventType.STEP_STARTED, {"step": step})
            step_result = await getattr(self, f"_step_{step}")(task, ctx)
            if step_result is not None:
                result = step_result
            ctx.emit(TaskEventType.STEP_COMPLETED, {"step": step})
        return result
