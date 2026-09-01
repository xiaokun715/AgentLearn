"""Execution Executor（设计说明书 §26 Runtime 生命周期）。

    CREATE → START → RUN → COLLECT → CLEANUP → DESTROY

无论 SUCCESS / FAIL / TIMEOUT / KILL，最后都必须 cleanup()。
这里用 try/finally 保证 runtime 与临时工作区一定被回收（§25 Ephemeral Runtime）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from ..domain.execution import Execution, ExecutionStatus
from ..domain.result import ExecutionResult
from ..filesystem.boundary import Workspace
from ..policy.compiler import SandboxConfig
from ..sandbox.base import Sandbox
from .resource import ResourceMonitor
from .timeout import wait_with_timeout

logger = logging.getLogger(__name__)


class ExecutionExecutor:
    """编排一次执行：建 runtime → 跑 → 收结果 → 一定回收。"""

    def __init__(self, sandbox: Sandbox, resource_monitor: ResourceMonitor | None = None) -> None:
        self.sandbox = sandbox
        self.resource_monitor = resource_monitor or ResourceMonitor()

    async def run(
        self,
        execution: Execution,
        config: SandboxConfig,
        kill_event: asyncio.Event,
        track_runtime=None,
    ) -> ExecutionResult:
        execution.status = ExecutionStatus.STARTING
        execution.started_at = datetime.now(timezone.utc)
        started = time.monotonic()

        workspace: Workspace | None = None
        runtime_id: str | None = None
        monitor_task: asyncio.Task | None = None
        stop_monitor: asyncio.Event | None = None
        reason: str | None = None
        exit_code: int | None = None
        stdout = stderr = ""
        resource_usage: dict = {}

        try:
            # ---------- CREATE ----------
            workspace = Workspace.create(execution.id, config.tool_type, execution.code)
            runtime_id = await self.sandbox.create(config, workspace)
            execution.runtime_id = runtime_id
            if track_runtime is not None:
                track_runtime(execution.id, runtime_id)

            # ---------- START ----------
            execution.status = ExecutionStatus.RUNNING
            await self.sandbox.start(runtime_id)

            # ---------- RUN ----------
            stop_monitor = asyncio.Event()

            async def breach_handler(breach_reason: str, message: str) -> None:
                logger.warning("resource breach [%s]: %s", breach_reason, message)
                await self.sandbox.kill(runtime_id, reason=breach_reason)

            monitor_task = asyncio.create_task(
                self.resource_monitor.run(
                    runtime_id, self.sandbox,
                    memory_mb=config.memory_mb,
                    pids=config.pids,
                    on_breach=breach_handler,
                    stop=stop_monitor,
                )
            )

            try:
                exit_code = await wait_with_timeout(
                    self.sandbox.wait(runtime_id),
                    config.timeout_seconds,
                    on_timeout=lambda: self.sandbox.kill(runtime_id, reason="timeout"),
                )
            except asyncio.TimeoutError:
                # 超时：已真正 kill（on_timeout），再取一次退出码（§21 采集 exit_code）
                reason = "timeout"
                exit_code = await self._wait_exit_after_kill(runtime_id)

            # 外部 Kill Switch 触发（§22）—— 优先级最高
            if execution.kill_requested or (kill_event is not None and kill_event.is_set()):
                reason = "killed"

            # 运行时自我终止的原因（OOM / PID / output_limit / ...）
            meta = await self.sandbox.meta(runtime_id)
            if reason is None:
                reason = meta.get("termination_reason")
            if reason is None and meta.get("oom_killed"):
                reason = "oom"

            stop_monitor.set()
            resource_usage = await monitor_task

            # ---------- COLLECT ----------
            stdout, stderr = await self.sandbox.collect(runtime_id)
            stdout, stderr, overflowed = _truncate_output(stdout, stderr, config.output_kb)
            if overflowed and reason is None:
                reason = "output_limit_exceeded"
                await self.sandbox.kill(runtime_id)

            return ExecutionResult(
                execution_id=execution.id,
                status=_final_status(reason, exit_code),
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=_error_text(reason),
                resource_usage=resource_usage,
            )

        except Exception as exc:  # noqa: BLE001 —— 建容器失败、start 失败等
            logger.exception("execution failed: %s", execution.id)
            return ExecutionResult(
                execution_id=execution.id,
                status=ExecutionStatus.FAILED,
                stdout="",
                stderr="",
                exit_code=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=str(exc),
                resource_usage={},
            )

        finally:
            # ---------- CLEANUP / DESTROY ----------（无论如何都执行）
            # 先停掉资源监控，避免遗留后台任务（§19）
            if stop_monitor is not None:
                stop_monitor.set()
            if monitor_task is not None and not monitor_task.done():
                monitor_task.cancel()
                try:
                    await monitor_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

            if runtime_id is not None:
                try:
                    await self.sandbox.destroy(runtime_id)
                except Exception:  # noqa: BLE001
                    logger.exception("destroy runtime failed (idempotent): %s", runtime_id)
            if workspace is not None:
                workspace.cleanup()


    async def _wait_exit_after_kill(self, runtime_id: str) -> int | None:
        """kill 之后等 runtime 退出并采集 exit_code（短超时，取不到就 None）。"""
        try:
            return await asyncio.wait_for(self.sandbox.wait(runtime_id), timeout=5.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return None


def _final_status(reason: str | None, exit_code: int | None) -> ExecutionStatus:
    """把终止原因映射为状态机终态（§8）。"""
    if reason == "killed":
        return ExecutionStatus.KILLED
    if reason == "timeout":
        return ExecutionStatus.TIMEOUT
    if reason == "oom":
        return ExecutionStatus.OOM
    if reason == "output_limit_exceeded":
        return ExecutionStatus.OUTPUT_LIMIT_EXCEEDED
    if reason == "pid":
        return ExecutionStatus.FAILED  # fork bomb 被 PID limit 拦下
    if exit_code == 0:
        return ExecutionStatus.SUCCEEDED
    return ExecutionStatus.FAILED


def _error_text(reason: str | None) -> str | None:
    return None if reason is None else f"terminated: {reason}"


def _truncate_output(stdout: str, stderr: str, output_kb: int) -> tuple[str, str, bool]:
    """输出上限（§24）：stdout+stderr 合计超过 output_kb 就截断并标记 overflowed。"""
    limit = output_kb * 1024
    total = len(stdout) + len(stderr)
    if total <= limit:
        return stdout, stderr, False
    if len(stdout) >= limit:
        stdout = stdout[:limit] + f"\n...[TRUNCATED at {limit} bytes]"
        stderr = ""
    else:
        stderr = stderr[: limit - len(stdout)] + "...[TRUNCATED]"
    return stdout, stderr, True
