"""example 02 —— 故障恢复：Worker 崩溃 -> Lease 过期 -> Reaper 接管 -> 断点续跑（§31 / §41）。

    python examples/failure_recovery.py

chaos_agent 会在 search 的 Tool 成功之后、step Checkpoint 之前「崩溃」。
这正是 §40 强调的最危险的窗口：Tool 已执行成功但 Checkpoint 未保存。
由于我们做了 write-ahead（Tool Execution Record 先落盘），恢复后 Tool 不会重复执行。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import QueueConfig
from app.factory import build_runtime

CRASH_JOB = {
    "query": "NVIDIA 最新财报",
    "chaos": {
        "crash_after_tool": True,   # tool 成功后模拟崩溃
        "llm_latency": 0.02,
        "tool_latency": 0.02,
    },
}


async def main() -> None:
    # 短租约 + 关闭自动 Reaper，便于我们手动控制恢复时机
    cfg = QueueConfig(
        worker_count=2, lease_duration=0.3, heartbeat_interval=0.1,
        reaper_interval=10, reaper_grace=0.0,
        storage_backend="sqlite", database_url="sqlite:///./data/failure_recovery.db",
    )
    rt = await build_runtime(cfg, start_reaper=False)
    await rt.start()
    try:
        job = await rt.job_service.create(agent="chaos_agent", input=CRASH_JOB)
        print(f"[create] {job.id}")

        # —— 等待 Worker 崩溃（RUNNING 且租约过期）——
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            j = await rt.job_service.get(job.id)
            if j.status.value == "running" and j.lease_expire_at and j.lease_expire_at < time.time():
                break
            await asyncio.sleep(0.02)
        j = await rt.job_service.get(job.id)
        print(f"[crash]  job 停留在 RUNNING（租约已过期），worker={j.worker_id}")

        # —— 崩溃前 Checkpoint 里已有哪些进度？——
        cp = await rt.checkpoint_store.load(job.id)
        print(f"[checkpoint] completed_steps={cp['completed_steps']}  tool_records={list(cp['tool_records'])}")

        # —— Reaper 发现过期租约，重新入队 ——
        recovered = await rt.pool.reaper.reap_once()
        print(f"[reaper] 回收 {recovered} 个 Job，重新入队")

        # —— 新 Worker 接管，从 Checkpoint 断点续跑 ——
        while j.status.value not in ("success", "failed", "cancelled", "dead"):
            await asyncio.sleep(0.05)
            j = await rt.job_service.get(job.id)
        print(f"[final]  status={j.status.value}  result_keys={list((j.result or {}).keys())}")

        events = await rt.job_service.events(job.id)
        types = [e.event_type for e in events]
        print(f"[idempotency] TOOL_CALLED={types.count('TOOL_CALLED')}  "
              f"TOOL_SKIPPED={types.count('TOOL_SKIPPED')}   <- 恢复后 Tool 未重复执行")
    finally:
        await rt.stop()


if __name__ == "__main__":
    asyncio.run(main())
