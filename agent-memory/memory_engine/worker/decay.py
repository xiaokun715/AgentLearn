"""遗忘与衰减 (Decay / Forget) 的后台调度器。

对齐说明书 §遗忘与衰减：定时任务模拟“大脑睡眠时的遗忘代谢”，
对边缘低活记忆调用 ``engine.run_decay_cycle`` 软失效。实现采用姊妹项目
async-agent-job-queue 的 reaper 模式：

* ``run_once()`` —— 手动跑一轮(测试 / 观测 API 直接调用)；
* ``run()``      —— ``while True: sleep(interval)`` 的无限循环(FastAPI lifespan 里 create_task)。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..config import AgentMemoryConfig
from ..engine import MemoryEngine
from ..records import MemoryRecord

logger = logging.getLogger("agentmemory.decay")


class DecayWorker:
    def __init__(self, engine: MemoryEngine, config: AgentMemoryConfig) -> None:
        self.engine = engine
        self.config = config
        self._task: Optional[asyncio.Task] = None
        self.sweeps: int = 0          # 已执行轮数(可观测)
        self.last_slept: list[MemoryRecord] = []  # 最近一轮沉睡的记录

    # ------------------------------------------------------------------
    def run_once(
        self,
        half_life_days: float | None = None,
        retention_floor: float | None = None,
    ) -> list[MemoryRecord]:
        """跑一轮遗忘代谢，返回被软失效(沉睡)的记忆。"""
        slept = self.engine.run_decay_cycle(
            half_life_days=(
                half_life_days if half_life_days is not None else self.config.half_life_days
            ),
            retention_floor=(
                retention_floor
                if retention_floor is not None
                else self.config.retention_floor
            ),
        )
        self.sweeps += 1
        self.last_slept = slept
        if slept:
            logger.info("decay sweep #%d: %d memories slept", self.sweeps, len(slept))
        return slept

    # ------------------------------------------------------------------
    async def run(self) -> None:
        """后台循环：每 interval 秒代谢一次(由 lifespan 启停)。"""
        interval = max(1.0, self.config.decay_interval_s)
        while True:
            await asyncio.sleep(interval)
            self.run_once()

    # ------------------------------------------------------------------
    def start(self) -> None:
        """创建后台任务(幂等)。"""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def stop(self) -> None:
        """取消后台任务。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
