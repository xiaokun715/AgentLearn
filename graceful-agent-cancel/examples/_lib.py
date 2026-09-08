"""examples 共享小工具：事件轮询 + 终端等待 + 打印排版。"""
from __future__ import annotations

import asyncio
import time
from typing import Callable


async def wait_for_event(rt, task_id: str, pred: Callable[[dict], bool],
                         timeout: float = 8.0, interval: float = 0.01):
    """轮询事件历史直到满足 pred，返回该事件 dict；超时返回 None。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for ev in rt.service.history(task_id):
            if pred(ev):
                return ev
        await asyncio.sleep(interval)
    return None


TERMINAL = {"TASK_COMPLETED", "TASK_CANCELLED", "TASK_FAILED"}


async def wait_terminal(rt, task_id: str, timeout: float = 10.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = rt.service.get(task_id)
        if task.status.is_terminal:
            return task
        await asyncio.sleep(interval)
    return rt.service.get(task_id)


def banner(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def show_events(rt, task_id: str, tail: int | None = None) -> None:
    evs = rt.service.history(task_id)
    if tail:
        evs = evs[-tail:]
    for e in evs:
        print(f"  #{e['seq']:<3} {e['type']:<24} {str(e.get('payload') or '')[:110]}")


def elapsed(t0: float) -> str:
    return f"{time.monotonic() - t0:.2f}s"
