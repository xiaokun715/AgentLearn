"""example 01 —— 循环层（第 2 道防线）:协作式取消，在两次动作之间优雅跳出。

    python examples/demo_1_cooperative.py

场景：Agent 正在跑「collect」(查账单+天气) 的时候，用户按了 Stop（cooperative）。
结果：Agent 先把**正在进行的天气查询跑完**，然后在下一个动作边界
     （即将开始慢速 fetch_web 之前）撞上 cancelled 标记主动跳出 ——
     **那个最贵的慢速网页抓取从未被启动**，帮你省下了 Token。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import AgentConfig
from app.factory import build_runtime
from examples._lib import banner, elapsed, show_events, wait_for_event, wait_terminal


async def main() -> None:
    # quick_tool_delay 放大，留出给“人/测试”按 Stop 的时间窗口
    cfg = AgentConfig().fast()
    cfg.quick_tool_delay = 0.5          # 让 collect 阶段足够慢，方便中途取消
    rt = build_runtime(cfg)

    t = rt.service.submit(query="查最近 2 天账单和上海天气，再抓网页核对出简报", days=2)

    # 等到“天气工具刚开始执行”时按下 cooperative 取消
    ev = await wait_for_event(rt, t.task_id,
                              lambda e: e["type"] == "TOOL_STARTED"
                              and (e["payload"] or {}).get("tool") == "fetch_weather")
    t0 = time.monotonic()
    print(f"[watch] 检测到 fetch_weather 开始 -> 用户按下 STOP (mode=cooperative)")
    res = rt.service.cancel(t.task_id, mode="cooperative")
    print(f"[cancel] note: {res['note']}")

    task = await wait_terminal(rt, t.task_id)
    print(f"[final] 状态={task.status.value}  用时={elapsed(t0)}")

    banner("关键证据：fetch_web（慢速 I/O）从未被启动")
    started_web = any(e["type"] == "TOOL_STARTED"
                      and (e["payload"] or {}).get("tool") == "fetch_web"
                      for e in rt.service.history(t.task_id))
    print(f"  TOOL_STARTED fetch_web 出现过吗？ -> {started_web}   (应为 False，贵动作被省掉了)")
    has_break = any(e["type"] == "LOOP_CANCEL_BREAK" for e in rt.service.history(t.task_id))
    print(f"  LOOP_CANCEL_BREAK 出现过吗？     -> {has_break}   (应为 True，层2 撞上边界)")
    print(f"  DB_ROLLED_BACK 出现过吗？        -> "
          f"{any(e['type']=='DB_ROLLED_BACK' for e in rt.service.history(t.task_id))}")

    banner("Partial Yield（兜底产出）")
    print(task.partial_yield or "(无)")

    banner("事件时间线（尾部）")
    show_events(rt, t.task_id, tail=14)


if __name__ == "__main__":
    asyncio.run(main())
