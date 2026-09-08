"""取消的两条核心链路：层2（动作边界协作式）与层3（force 掐断 I/O），外加边界情形。"""
from __future__ import annotations

import time

import pytest

from tests.util import started_tool, types_of, wait_event, wait_terminal


async def _wait_tool_started(rt, tid, tool):
    return await wait_event(rt, tid,
                            lambda e: e["type"] == "TOOL_STARTED"
                            and (e["payload"] or {}).get("tool") == tool)


# ---- 层2：cooperative 在动作边界跳出，最贵的慢速 I/O 从未被启动 ----
async def test_cooperative_breaks_before_expensive_io(make_runtime):
    rt = make_runtime(quick_tool_delay=0.5)
    t = rt.service.submit(query="查账单天气再抓网页", days=2)

    await _wait_tool_started(rt, t.task_id, "fetch_weather")   # 趁 weather 还在跑时取消
    rt.service.cancel(t.task_id, mode="cooperative")
    task = await wait_terminal(rt, t.task_id)

    assert task.status.value == "cancelled"
    assert started_tool(rt, t.task_id, "fetch_web") is False    # fetch_web 从未启动
    assert "LOOP_CANCEL_BREAK" in types_of(rt, t.task_id)
    assert "TEMP_CLEANED" in types_of(rt, t.task_id)
    assert task.partial_yield and "账单" in task.partial_yield


# ---- 层3：force 在 Agent 卡进 4s 慢 I/O 时，当场掐断 ----
async def test_force_interrupts_slow_io(make_runtime):
    rt = make_runtime(web_fetch_total=4.0)
    t = rt.service.submit(query="抓一个很慢的网页", days=1)

    await _wait_tool_started(rt, t.task_id, "fetch_web")
    t0 = time.monotonic()
    rt.service.cancel(t.task_id, mode="force")
    task = await wait_terminal(rt, t.task_id, timeout=3.0)
    dt = time.monotonic() - t0

    assert task.status.value == "cancelled"
    assert dt < 2.0, f"force 取消不应等那 4s 的 I/O 跑完，实际用了 {dt:.2f}s"
    assert "FORCE_CANCELLED" in types_of(rt, t.task_id)
    assert task.partial_yield and "中止" in task.partial_yield


# ---- 边界：任务协程还没开始跑就被 force 取消（asyncio 不执行协程体）----
async def test_force_cancel_before_start(runtime):
    t = runtime.service.submit(query="秒取消", days=1)
    rt = runtime
    action = rt.service.cancel(t.task_id, mode="force")["action"]
    assert action["pre_start"] is True

    task = await wait_terminal(rt, t.task_id, timeout=2.0)
    assert task.status.value == "cancelled"
    assert task.cancel_requested is True
    assert "FORCE_CANCELLED" in types_of(rt, t.task_id)
    assert "TASK_CANCELLED" in types_of(rt, t.task_id)
    # 没有事实 -> Partial Yield 给出友好提示而不是红字报错
    assert task.partial_yield and "尚未收集到任何有效信息" in task.partial_yield


# ---- 非法 mode 被拒 ----
async def test_invalid_cancel_mode_rejected(runtime):
    t = runtime.service.submit(query="查一下", days=1)
    with pytest.raises(ValueError):
        runtime.service.cancel(t.task_id, mode="force_plus_delete")
