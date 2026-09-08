"""测试共享工具：快速配置 + 事件轮询 + 终端等待。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Callable

from app.config import AgentConfig

TERMINAL = ("completed", "cancelled", "failed")


def make_config(**overrides) -> AgentConfig:
    """基于 fast 配置覆盖若干时延/参数，保证测试快又稳。"""
    base = AgentConfig().fast()
    return replace(base, **overrides)


async def wait_event(rt, task_id: str, pred: Callable[[dict], bool],
                     timeout: float = 10.0, interval: float = 0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for ev in rt.service.history(task_id):
            if pred(ev):
                return ev
        await asyncio.sleep(interval)
    return None


async def wait_terminal(rt, task_id: str, timeout: float = 10.0, interval: float = 0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = rt.service.get(task_id)
        if task.status.value in TERMINAL:
            return task
        await asyncio.sleep(interval)
    return rt.service.get(task_id)


def types_of(rt, task_id: str) -> list[str]:
    return [e["type"] for e in rt.service.history(task_id)]


def started_tool(rt, task_id: str, tool: str) -> bool:
    return any(e["type"] == "TOOL_STARTED" and (e["payload"] or {}).get("tool") == tool
               for e in rt.service.history(task_id))
