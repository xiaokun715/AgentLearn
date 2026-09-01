"""Timeout 处理（设计说明书 §21）。

核心：超时后**必须真正 kill runtime**，而不是只停止等待。
否则会出现：
    API 返回 timeout，但容器 / 进程还在跑 → 资源泄漏。

用法（executor 内）：
    try:
        exit_code = await asyncio.wait_for(sandbox.wait(runtime_id), timeout=cfg.timeout_seconds)
    except asyncio.TimeoutError:
        await sandbox.kill(runtime_id)      # ← 关键一步
        status = TIMEOUT
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def wait_with_timeout(
    coro: Awaitable[T],
    timeout_seconds: float,
    on_timeout: Callable[[], Awaitable[None]],
) -> T:
    """在 timeout 内等待 coro；超时先执行 on_timeout（真正杀掉 runtime）再抛异常。"""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("execution timed out after %.1fs; killing runtime", timeout_seconds)
        try:
            await on_timeout()
        except Exception:  # noqa: BLE001
            logger.exception("on_timeout (kill) failed")
        raise
