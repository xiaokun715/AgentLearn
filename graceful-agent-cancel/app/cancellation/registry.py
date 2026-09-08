"""RunningTaskRegistry —— 取消指令的执行者（层2 与层3 的开关都在这里）。

收到用户 Stop（层1 -> CANCEL_TASK）后，网关调用 :meth:`request_cancel`：

- ``mode="cooperative"``：只在取消存储里标记 ``cancelled``。
  Agent Loop 在**下一个动作边界**撞上埋点，主动跳出 —— 只影响“两次动作之间”（层2）。
  代价：如果 Agent 正卡在一个 20 秒的网络 I/O 里，它要等这个动作完成才停。

- ``mode="force"``：标记 ``cancelled`` **并** 调用 ``asyncio.Task.cancel()``。
  Python 事件循环把 CancelledError 抛进协程当前 await 点，直接掐断 TCP/HTTP I/O（层3）。
  用户无需等 20 秒，Token 计费也立刻停。

生产里“Stop 按钮”通常 = force；cooperative 用于让你只观察层2 一条链路。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..store.base import CancellationStore

CANCEL_MODES = ("cooperative", "force")


@dataclass
class TaskHandle:
    """一个正在执行的 agent（asyncio.Task）对应的运行时句柄。"""

    task_id: str
    coro: asyncio.Task
    cancel_requested: bool = False
    # Agent 协程体是否已经真正开始执行（由 _drive 首行置位）。
    # False 且被 force 取消 => “启动前即被取消”，asyncio 不会执行协程体，
    # 上层必须自行善后。
    started: bool = False


class RunningTaskRegistry:
    def __init__(self, store: CancellationStore) -> None:
        self._store = store
        self._handles: dict[str, TaskHandle] = {}

    def register(self, task_id: str, coro: asyncio.Task) -> None:
        self._handles[task_id] = TaskHandle(task_id=task_id, coro=coro)

    def unregister(self, task_id: str) -> None:
        self._handles.pop(task_id, None)

    def get(self, task_id: str) -> TaskHandle | None:
        return self._handles.get(task_id)

    def mark_started(self, task_id: str) -> None:
        """由 Agent 协程体的第一行调用：证明它真的开始执行了。"""
        h = self._handles.get(task_id)
        if h is not None:
            h.started = True

    def is_running(self, task_id: str) -> bool:
        h = self._handles.get(task_id)
        return h is not None and not h.coro.done()

    def running_task_ids(self) -> list[str]:
        """当前仍在执行的 agent 任务（供优雅停机/监控遍历）。"""
        return [tid for tid, h in self._handles.items() if not h.coro.done()]

    def store(self) -> CancellationStore:
        return self._store

    # ---- 取消（本章核心） -----------------------------------------------------
    def request_cancel(self, task_id: str, mode: str = "force") -> dict:
        """按 mode 执行取消，返回实际采取的动作。"""
        assert mode in CANCEL_MODES, f"mode 必须是 {CANCEL_MODES} 之一，got {mode!r}"

        # 层2 素材：无论哪种模式，先把“Redis 状态”标记成 cancelled，
        # 这样 Agent Loop 在下一个动作边界查询时就能撞上埋点跳出循环。
        self._store.mark_cancelled(task_id)

        handle = self._handles.get(task_id)
        if handle is None:
            return {"token": True, "coro_cancel": False, "reason": "no running handle"}

        handle.cancel_requested = True

        # 层3：force 才真正向正在执行的协程抛出取消信号
        if mode == "force" and not handle.coro.done():
            handle.coro.cancel()
            return {
                "token": True, "coro_cancel": True,
                # 若协程体还没开始跑（started=False），asyncio 会让任务直接以
                # cancelled 结束、**根本不执行协程体**，supervisor 无从介入，
                # 上层需要自行善后（见 service._finalize_prestart）。
                "pre_start": not handle.started,
                "reason": "asyncio.Task.cancel()",
            }

        return {"token": True, "coro_cancel": False,
                "reason": "cooperative: break at next loop boundary"}
