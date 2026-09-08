"""Partial Yield（部分产出）的友好话术与元数据。"""
from __future__ import annotations

from tests.util import wait_event, wait_terminal


async def test_partial_yield_after_cooperative_has_collected_facts(make_runtime):
    rt = make_runtime(quick_tool_delay=0.4)
    t = rt.service.submit(query="查一下账单和天气", days=2)

    await wait_event(rt, t.task_id,
                     lambda e: e["type"] == "TOOL_STARTED"
                     and (e["payload"] or {}).get("tool") == "fetch_weather")
    rt.service.cancel(t.task_id, mode="cooperative")
    task = await wait_terminal(rt, t.task_id)

    assert task.partial_yield
    assert "已根据您的要求中止" in task.partial_yield
    assert "[账单" in task.partial_yield               # 已收集事实被列出来
    assert "剩余动作" in task.partial_yield            # 明确说明“剩余已取消”
    assert task.partial_meta["cause"] == "cooperative"
    assert task.partial_meta["facts_count"] >= 2
    assert isinstance(task.partial_meta["db_rows_rolled_back"], int)


async def test_partial_yield_lists_facts_and_cancels_remaining(make_runtime):
    rt = make_runtime(quick_tool_delay=0.4)
    t = rt.service.submit(query="账单", days=1)
    await wait_event(rt, t.task_id,
                     lambda e: e["type"] == "TOOL_STARTED"
                     and (e["payload"] or {}).get("tool") == "fetch_weather")
    rt.service.cancel(t.task_id, mode="cooperative")
    task = await wait_terminal(rt, t.task_id)

    # 每条已收集事实都应该出现在话术里
    for fact in task.facts:
        assert fact["text"] in task.partial_yield
    # Partial 内容也通过事件广播给了订阅者
    partial_ev = next((e for e in rt.service.history(t.task_id)
                       if e["type"] == "PARTIAL_YIELD"), None)
    assert partial_ev is not None and "中止" in partial_ev["payload"]["text"]
