"""example 03 —— 兜底层（第 4 道防线）：取消发生在“写库/写文件”中途 -> 回滚 + Partial Yield。

    python examples/demo_3_partial_yield.py

场景：Agent 已经走到最后一步 finalize，正在把每条事实逐行写进「临时表 / 临时文件」
      （模拟脏数据）。此刻 force 取消：
        - SimLedger.rollback()   丢弃未提交的临时表行（等价事务回滚）
        - ReportWorkspace.cleanup()  删除残留的临时报告文件
        - 再基于已收集事实输出一段友好的“部分产出”，而不是冷冰冰报错。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import AgentConfig
from app.factory import build_runtime
from examples._lib import banner, show_events, wait_for_event, wait_terminal


async def main() -> None:
    cfg = AgentConfig().fast()
    cfg.finalize_row_delay = 0.15       # 放大“写每一行”的间隔，让取消能落在中途
    cfg.finalize_delay = 0.12
    rt = build_runtime(cfg)

    t = rt.service.submit(query="查账单天气并抓网页", days=2)

    ev = await wait_for_event(rt, t.task_id, lambda e: e["type"] == "FINALIZE_STAGING")
    print("[watch] 检测到开始写临时表/临时文件 -> 用户按下 STOP (mode=force)")
    rt.service.cancel(t.task_id, mode="force")

    task = await wait_terminal(rt, t.task_id)
    print(f"[final] 状态={task.status.value}")

    evs = rt.service.history(t.task_id)
    rollback = next((e for e in evs if e["type"] == "DB_ROLLED_BACK"), None)
    cleaned = next((e for e in evs if e["type"] == "TEMP_CLEANED"), None)
    print(f"  DB_ROLLED_BACK -> {rollback and rollback['payload']}   (回滚掉的脏行)")
    print(f"  TEMP_CLEANED   -> {cleaned and cleaned['payload']}   (清理掉的临时文件)")

    banner("Partial Yield —— 给用户看的友好快照")
    print(task.partial_yield or "(无)")

    banner("事件时间线（尾部）")
    show_events(rt, t.task_id, tail=16)


if __name__ == "__main__":
    asyncio.run(main())
