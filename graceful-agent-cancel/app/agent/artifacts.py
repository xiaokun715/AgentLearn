"""Partial Yield（部分产出）拼装 —— 第十章 4.2 节。

被“暴力”叫停后，不要甩给用户一个冰冷的 “Task Cancelled” 红字报错；
而是基于**已经收集到的事实**给一份友好快照：“在停止前我已经为您查到……剩余操作已取消”。
"""
from __future__ import annotations

from ..domain.task import AgentTask

_CAUSE_CN = {
    "cooperative": "在两次动作的间隙被优雅拦截",
    "force": "底层已切断仍在进行的网络请求（asyncio cancel）",
    "error": "任务异常终止",
}


def build_partial(task: AgentTask, cause: str, stats: dict) -> tuple[str, dict]:
    """返回 (话术文本, 元数据)。元数据同时写回 task.partial_meta 供查询。"""
    rows = stats["db_rows_rolled_back"]
    files = stats["temp_files_removed"]
    cause_cn = _CAUSE_CN.get(cause, cause)

    if task.facts:
        bullets = "\n".join(f"  - {f['text']}" for f in task.facts)
        collected = f"在停止前，我已经为您查到以下信息：\n{bullets}"
    else:
        collected = "很遗憾，在停止前尚未收集到任何有效信息。"

    # 已回滚 / 已清理的善后说明
    tidy = []
    if rows:
        tidy.append(f"本次中止已自动回滚临时数据表（{rows} 行），未留下脏数据")
    if files:
        tidy.append(f"并清理了 {len(files)} 个临时文件")
    tidy_note = "；".join(tidy) + "。" if tidy else ""

    text = (
        "您好，任务已根据您的要求中止（"
        f"{cause_cn}）。\n{collected}\n"
        "剩余动作（继续抓取、写库、生成完整报告等）已取消。"
        f"{tidy_note}"
    )

    meta = {
        "cause": cause,
        "cause_cn": cause_cn,
        "facts_count": len(task.facts),
        "db_rows_rolled_back": rows,
        "temp_files_removed": files,
    }
    return text, meta
