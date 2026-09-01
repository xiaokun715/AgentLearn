"""Resource Monitor（设计说明书 §19-20）。

至少限制：CPU / Memory / Timeout / PID / Disk / Output。
这里周期性采集 runtime 的 CPU / 内存 / 进程数，聚合成 peak 值，并在越限时触发 kill。

越限回调让 executor 决定最终状态：
    - memory 越限 → OOM
    - pids 越限   → FAILED（fork bomb 被抓）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

BreachHandler = Callable[[str, str], Awaitable[None]]  # (reason, message)


class ResourceMonitor:
    """周期采样 + 越限回调。"""

    def __init__(self, interval: float = 0.5) -> None:
        self.interval = interval

    async def run(
        self,
        runtime_id: str,
        sandbox,
        *,
        memory_mb: int,
        pids: int,
        on_breach: BreachHandler,
        stop: asyncio.Event,
    ) -> dict:
        """持续监控直到 stop 置位或越限。返回峰值用量。"""
        peak_cpu = 0.0
        peak_mem = 0.0
        peak_pids = 0

        while not stop.is_set():
            stats = await sandbox.stats(runtime_id)
            cpu = float(stats.get("cpu_percent", 0.0))
            mem = float(stats.get("memory_mb", 0.0))
            pid_count = int(stats.get("pids", 0))

            peak_cpu = max(peak_cpu, cpu)
            peak_mem = max(peak_mem, mem)
            peak_pids = max(peak_pids, pid_count)

            if mem > memory_mb:
                logger.warning("resource breach: memory %.1fMB > %dMB (runtime=%s)",
                               mem, memory_mb, runtime_id)
                await on_breach("oom", f"memory {mem:.0f}MB > {memory_mb}MB")
                break
            if pids > 0 and pid_count > pids:
                logger.warning("resource breach: pids %d > %d (runtime=%s)",
                               pid_count, pids, runtime_id)
                await on_breach("pid", f"processes {pid_count} > {pids}")
                break

            await asyncio.sleep(self.interval)

        return {
            "cpu_percent": round(peak_cpu, 2),
            "memory_peak_mb": round(peak_mem, 2),
            "pids_peak": peak_pids,
        }
