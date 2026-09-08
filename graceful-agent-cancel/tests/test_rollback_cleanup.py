"""层4 兜底：事务回滚 + 临时文件清理（写库/写文件中途被取消时不留脏数据）。"""
from __future__ import annotations

import asyncio

from app.resources.db import SimLedger
from app.resources.workspace import DEMO_BASE, ReportWorkspace
from tests.util import types_of, wait_event, wait_terminal


# ---- 单元：SimLedger 的 stage/commit/rollback 语义 ----
async def test_sim_ledger_rollback_discards_staged_rows():
    db = SimLedger()
    db.stage({"kind": "bill"})
    db.stage({"kind": "weather"})
    assert db.staged_count == 2
    dropped = db.rollback()
    assert dropped == 2
    assert db.staged_count == 0
    assert db.committed_count == 0          # 正式表没有任何行 -> 不留脏数据


async def test_sim_ledger_commit_persists():
    db = SimLedger()
    db.stage({"kind": "bill"})
    assert db.commit() == 1
    assert db.staged_count == 0
    assert db.committed_count == 1


# ---- 单元：ReportWorkspace 的发布 / 清理 ----
async def test_workspace_publish_and_cleanup():
    ws = ReportWorkspace("ws_ut_1", base_dir=DEMO_BASE)
    ws.create()
    ws.append_line("# report")
    assert len(ws.staged_files()) == 1
    final = ws.publish()
    assert ws.published
    assert ws.work_dir.exists() is False      # 临时工作区已被删除
    import os
    assert os.path.exists(final)

    ws2 = ReportWorkspace("ws_ut_2", base_dir=DEMO_BASE)
    ws2.create()
    ws2.append_line("half")
    info = ws2.cleanup()
    assert info is not None and len(info["removed_files"]) == 1
    assert ws2.work_dir.exists() is False


# ---- 集成：在 finalize 写库途中 force 取消 -> 回滚 + 清理 + Partial ----
async def test_cancel_during_finalize_rolls_back(make_runtime):
    rt = make_runtime(finalize_row_delay=0.12, finalize_delay=0.1)
    t = rt.service.submit(query="写到一半就取消", days=2)

    ev = await wait_event(rt, t.task_id, lambda e: e["type"] == "FINALIZE_STAGING")
    assert ev is not None
    rt.service.cancel(t.task_id, mode="force")
    task = await wait_terminal(rt, t.task_id)

    assert task.status.value == "cancelled"
    assert task.result is None                      # 没有产出正式结果

    rollback = next((e for e in rt.service.history(t.task_id)
                     if e["type"] == "DB_ROLLED_BACK"), None)
    assert rollback is not None
    assert rollback["payload"]["rows_dropped"] >= 1  # 临时表里已写的行被回滚

    cleaned = next((e for e in rt.service.history(t.task_id)
                    if e["type"] == "TEMP_CLEANED"), None)
    assert cleaned is not None                       # 临时文件被清理

    evs = types_of(rt, t.task_id)
    assert "TASK_COMPLETED" not in evs               # 没有“假装成功”
