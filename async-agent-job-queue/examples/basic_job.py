"""example 01 —— 基本 Job 生命周期（设计说明书 §8-9）。

    python examples/basic_job.py

演示：POST /v1/jobs 立即返回 job_id -> 后台 Worker 异步执行
      -> 轮询 GET /v1/jobs/{id} -> 查看事件 / 指标。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import QueueConfig
from app.factory import build_runtime


async def main() -> None:
    # 零依赖：SQLite（持久化）+ asyncio 内存队列
    rt = await build_runtime(QueueConfig())
    await rt.start()
    try:
        # —— 创建任务：立即返回，绝不等待 Agent 执行（§8）——
        job = await rt.job_service.create(
            agent="research_agent",
            input={"query": "分析 NVIDIA 最新财报"},
            tenant_id="acme",
        )
        print(f"[create] {job.id}  status={job.status.value}   (立即返回)")

        # —— 轮询直到终态（等价于 GET /v1/jobs/{id}）——
        while job.status.value not in ("success", "failed", "cancelled", "dead"):
            await asyncio.sleep(0.1)
            job = await rt.job_service.get(job.id)
            if job.status.value == "running":
                print(f"[poll]   status=running step={job.current_step} progress={job.progress}%")

        print(f"[final]  status={job.status.value}  retry_count={job.retry_count}")
        print(f"[result] {job.result}")

        # —— 事件历史（§36：State=快照，Event=历史）——
        print("\n[events]")
        for e in await rt.job_service.events(job.id):
            print(f"  {e.event_type:<18} {e.payload or ''}")

        # —— 指标（§46）——
        snap = rt.metrics.snapshot()
        print("\n[metrics]")
        for name, val in snap["counters"].items():
            if val:
                print(f"  {name} = {val}")
    finally:
        await rt.stop()


if __name__ == "__main__":
    asyncio.run(main())
