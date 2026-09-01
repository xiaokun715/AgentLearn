"""example 03 —— Agent 任务：逐步执行 + 逐步 Checkpoint + Tool 幂等（§16-23）。

    python examples/agent_job.py

展示 ResearchAgent 的 4 个步骤：
    analyze -> search(Tool) -> analyze_result -> generate_report
每完成一步保存一个 Checkpoint，中间穿插 TOOL_CALLED / CHECKPOINT_SAVED 事件。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import QueueConfig
from app.factory import build_runtime


async def main() -> None:
    cfg = QueueConfig(worker_count=1, storage_backend="memory", queue_backend="memory")
    rt = await build_runtime(cfg)
    await rt.start()
    try:
        job = await rt.job_service.create(
            agent="research_agent",
            input={"query": "2024 年全球 GPU 市场规模"},
            tenant_id="t1",
        )
        print(f"[create] {job.id}")

        # 逐步打印事件，观察 Checkpoint 节奏（§20）
        seen = set()
        while job.status.value not in ("success", "failed", "cancelled", "dead"):
            await asyncio.sleep(0.05)
            job = await rt.job_service.get(job.id)
            for e in await rt.job_service.events(job.id):
                if e.seq not in seen:
                    seen.add(e.seq)
                    if e.event_type in ("STEP_STARTED", "TOOL_CALLED", "TOOL_COMPLETED",
                                        "CHECKPOINT_SAVED", "JOB_COMPLETED"):
                        print(f"  {e.event_type:<18} {e.payload}")

        print(f"\n[final] status={job.status.value}")
        report = (job.result or {}).get("report", "")
        print(f"[report] {report[:120]}...")
    finally:
        await rt.stop()


if __name__ == "__main__":
    asyncio.run(main())
