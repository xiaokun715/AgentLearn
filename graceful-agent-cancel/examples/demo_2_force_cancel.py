"""example 02 —— 底层（第 3 道防线）：force 取消直接掐断卡在 I/O 里的协程。

    python examples/demo_2_force_cancel.py

场景：Agent 已经卡在一个「慢速网页抓取 fetch_web（模拟 20s 网络 I/O）」里。
      此时 cooperative 取消要等它爬完才停（白白等 + 烧 Token）；
      force 取消会调用 ``asyncio.Task.cancel()`` —— 事件循环当场把 CancelledError
      抛进正在 await 的那一行，掐断 I/O。
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
    cfg = AgentConfig().fast()
    cfg.web_fetch_total = 5.0           # 把“网页”调慢到 5 秒，制造一个卡住的 I/O
    rt = build_runtime(cfg)

    t = rt.service.submit(query="查账单天气并抓网页", days=2)

    # 等到 Agent 真的开始抓网页（正卡在里面）
    ev = await wait_for_event(rt, t.task_id,
                              lambda e: e["type"] == "TOOL_STARTED"
                              and (e["payload"] or {}).get("tool") == "fetch_web")
    t0 = time.monotonic()
    print(f"[watch] 检测到 fetch_web 开始（它要跑 {cfg.web_fetch_total}s）-> 用户按下 STOP (mode=force)")
    rt.service.cancel(t.task_id, mode="force")

    task = await wait_terminal(rt, t.task_id)
    dt = time.monotonic() - t0
    print(f"[final] 状态={task.status.value}  <-- 从按下 Stop 到停下只用了 {dt:.2f}s，"
          f"没有等那 {cfg.web_fetch_total}s 爬完")

    forced = any(e["type"] == "FORCE_CANCELLED" for e in rt.service.history(t.task_id))
    cleaned = any(e["type"] == "TEMP_CLEANED" for e in rt.service.history(t.task_id))
    print(f"  FORCE_CANCELLED 出现过吗？  -> {forced}   (层3 掐断)")
    print(f"  TEMP_CLEANED    出现过吗？  -> {cleaned}   (层4 清理临时文件)")

    banner("Partial Yield（部分产出）")
    print(task.partial_yield or "(无)")

    banner("事件时间线（尾部）")
    show_events(rt, t.task_id, tail=16)


if __name__ == "__main__":
    asyncio.run(main())
