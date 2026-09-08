"""正常生命周期（不取消）+ 终态后再取消是无害操作。"""
from __future__ import annotations

import os

from tests.util import types_of, wait_terminal


async def test_run_to_completion(runtime):
    t = runtime.service.submit(query="查账单并出报告", days=2)
    task = await wait_terminal(runtime, t.task_id)

    assert task.status.value == "completed"
    assert task.error is None
    assert task.result is not None
    assert task.result["committed_rows"] == 4          # 2 账单 + 1 天气 + 1 网页
    assert os.path.exists(task.result["report_file"])  # 成品已发布到 results/

    evs = types_of(runtime, t.task_id)
    assert "TASK_STARTED" in evs
    assert "STEP_STARTED" in evs
    assert "FINALIZE_STAGING" in evs
    assert "TASK_COMPLETED" in evs
    # 正常完成时不该有取消链路事件
    assert "FORCE_CANCELLED" not in evs
    assert "DB_ROLLED_BACK" not in evs


async def test_cancel_after_terminal_is_noop(runtime):
    t = runtime.service.submit(query="查账单", days=1)
    await wait_terminal(runtime, t.task_id)
    res = runtime.service.cancel(t.task_id, mode="force")
    assert res["status"] == "completed"
    assert "终态" in res["note"]


async def test_get_task_snapshot_has_partial_fields(runtime):
    t = runtime.service.submit(query="快照字段", days=1)
    await wait_terminal(runtime, t.task_id)
    pub = runtime.service.get(t.task_id).to_public()
    for key in ("task_id", "status", "query", "facts", "facts_count",
                "partial_yield", "result", "created_at", "finished_at"):
        assert key in pub
    assert pub["facts_count"] >= 2
