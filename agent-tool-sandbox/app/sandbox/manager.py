"""Sandbox Manager（设计说明书 §22 Kill Switch + §25 Ephemeral Runtime）。

职责：
1. 按配置选择沙箱后端（auto / docker / process），auto 时 Docker 不可用自动退化；
2. 维护 execution_id → runtime_id 的映射，支持幂等 kill switch；
3. 并发度控制（简单的 Execution Queue，§5；生产可用 Redis 队列替换）。
"""
from __future__ import annotations

import asyncio
import logging

from ..config import AppConfig
from ..domain.exceptions import SandboxBackendUnavailable
from .base import Sandbox
from .docker import DockerSandbox
from .process import ProcessSandbox

logger = logging.getLogger(__name__)


class SandboxManager:
    """编排沙箱生命周期，向 Service / API 提供统一入口。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._sandbox: Sandbox | None = None
        self._runtimes: dict[str, str] = {}            # execution_id -> runtime_id
        self._kill_events: dict[str, asyncio.Event] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    # ------------------------------------------------------------- backend
    def get_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            self._sandbox = self._build_sandbox()
        return self._sandbox

    def _build_sandbox(self) -> Sandbox:
        backend = self.config.sandbox_backend
        if backend == "process":
            return ProcessSandbox()
        if backend == "docker":
            return DockerSandbox()  # 失败会抛 SandboxBackendUnavailable
        # auto：优先 Docker，不可用退化到 process
        try:
            return DockerSandbox()
        except SandboxBackendUnavailable as exc:
            logger.warning("Docker 不可用 (%s)；退化到 ProcessSandbox", exc)
            return ProcessSandbox()

    # ----------------------------------------------------------- bookkeeping
    def register(self, execution_id: str) -> asyncio.Event:
        """为一次执行注册 kill 信号。返回 kill_event，executor 用它感知 kill。"""
        event = self._kill_events.get(execution_id)
        if event is None:
            event = asyncio.Event()
            self._kill_events[execution_id] = event
        return event

    def track(self, execution_id: str, runtime_id: str) -> None:
        """记录 execution → runtime 的映射，供 kill 路由定位真实进程/容器。"""
        self._runtimes[execution_id] = runtime_id

    def untrack(self, execution_id: str) -> None:
        self._runtimes.pop(execution_id, None)
        self._kill_events.pop(execution_id, None)

    # ------------------------------------------------------------- kill switch
    async def kill(self, execution_id: str) -> None:
        """幂等 kill（§22-23）：置位 kill_event，并真正终止 runtime。

        幂等性保证：重复 kill 不会 500，只会得到同一个 killed 结果。
        """
        event = self._kill_events.get(execution_id)
        if event is not None and not event.is_set():
            event.set()
        runtime_id = self._runtimes.get(execution_id)
        if runtime_id is not None:
            try:
                await self.get_sandbox().kill(runtime_id, reason="killed")
            except Exception:  # noqa: BLE001
                logger.debug("kill runtime %s failed (idempotent)", runtime_id)

    # ------------------------------------------------------------- concurrency
    async def gate(self, coro):
        """在并发配额内执行（简单执行队列：最多 max_concurrency 个并行沙箱）。"""
        async with self._semaphore:
            return await coro
