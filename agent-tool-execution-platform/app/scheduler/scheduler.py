"""Tool Scheduler —— 按执行模型分流（说明书 §4 / §54）。

Scheduler 是 Gateway 与具体执行路径之间**唯一的分叉点**：

::

    Tool Gateway
        │
        ▼
    Tool Scheduler
        │
        ├── execution_mode == sync  ──► Sync Executor（Agent 直接等结果）
        │
        └── execution_mode == async ──► Async Executor（入队，立刻返回 job_id）

分流的依据是 :class:`~app.domain.models.ToolMetadata` 里声明的 ``execution_mode``，
而不是「跑起来看它慢不慢」—— 执行模式必须是**声明式**的，
因为 Agent 需要在提交之前就知道「我这次会不会马上拿到结果」，
否则它无法决定要不要写 Checkpoint 然后放手（§49）。

Scheduler 还负责 Worker 池的生命周期：异步任务得有进程消费队列，
而 Worker 的启停应该由平台统一管理，不能散落在调用方。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import AppConfig
from ..domain.enums import ExecutionMode, ExecutionStatus, RiskLevel
from ..domain.models import SubmitResult, ToolCall
from ..infra.clock import Clock, SystemClock
from ..infra.redis import RedisSim
from ..tools.registry import ToolRegistry, ToolSpec
from .async_executor import AsyncExecutor
from .sync_executor import ExecutionOutcome, SyncExecutor
from .worker import Worker

logger = logging.getLogger(__name__)


@dataclass
class DispatchOutcome:
    """Scheduler 的裁决结果：要么同步跑完了，要么已经排进队列。"""

    mode: ExecutionMode
    sync: Optional[ExecutionOutcome] = None
    job_id: Optional[str] = None

    @property
    def accepted(self) -> bool:
        """是否走了异步路径（任务已入队，Agent 该去写 Checkpoint 了）。"""
        return self.job_id is not None

    def to_submit_result(self) -> SubmitResult:
        if self.sync is not None:
            return self.sync.to_submit_result(mode=self.mode)
        return SubmitResult(
            call_id="",
            status=ExecutionStatus.QUEUED,
            outcome="ACCEPTED",
            execution_mode=ExecutionMode.ASYNC,
            job_id=self.job_id,
        )


class Scheduler:
    """执行路径分流器 + Worker 池管理器。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        config: AppConfig,
        sync_executor: SyncExecutor,
        async_executor: Optional[AsyncExecutor] = None,
        sandbox_manager: Any = None,
        redis: Optional[RedisSim] = None,
        audit: Any = None,
        metrics: Any = None,
        worker_factory: Optional[Any] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.sync_executor = sync_executor
        self.async_executor = async_executor
        self.sandbox = sandbox_manager
        self.redis = redis
        self.audit = audit
        self.metrics = metrics
        self.worker_factory = worker_factory
        self.clock = clock or SystemClock()

        self.workers: list[Worker] = []
        self._cancelled: set[str] = set()

    # ==================================================================
    # 分流
    # ==================================================================
    def dispatch(
        self,
        call: ToolCall,
        spec: ToolSpec,
        *,
        idempotency_key: str = "",
        force_mode: Optional[ExecutionMode] = None,
    ) -> DispatchOutcome:
        """按 ``execution_mode`` 分流。"""
        mode = force_mode or spec.metadata.execution_mode

        if mode == ExecutionMode.SYNC:
            if self.audit:
                self.audit.record(
                    call.call_id, "scheduler.dispatched",
                    mode=mode.value, tool_name=call.tool_name,
                )
            outcome = self.sync_executor.run(call, spec, idempotency_key=idempotency_key)
            outcome.detail.setdefault("mode", mode.value)
            return DispatchOutcome(mode=mode, sync=outcome)

        # ---- 异步：入队即返回 ----
        if self.async_executor is None:
            raise RuntimeError(
                f"Tool {call.tool_name} 声明为异步执行，但 Scheduler 未配置 AsyncExecutor"
            )
        assert self.async_executor is not None  # 供类型检查器
        job_id = self.async_executor.submit(
            call,
            risk_level=spec.metadata.risk_level.value,
            timeout_ms=spec.metadata.timeout_ms,
        )
        if self.audit:
            self.audit.record(
                call.call_id, "scheduler.dispatched",
                mode=mode.value, tool_name=call.tool_name, job_id=job_id,
            )
        if self.metrics is not None:
            self.metrics.observe("tool_queue_wait_ms", 0.0, tool=call.tool_name)
        return DispatchOutcome(mode=mode, job_id=job_id)

    # ==================================================================
    # 取消（§41 PROCESSING -> CANCELLING -> CANCELLED）
    # ==================================================================
    def cancel(self, call_id: str) -> bool:
        """取消一次执行：杀沙箱 + 通知所有 Worker 跳过。幂等。"""
        self._cancelled.add(call_id)
        killed = False
        if self.sandbox is not None:
            try:
                killed = bool(self.sandbox.cancel(call_id, reason="cancelled"))
            except Exception:  # noqa: BLE001
                logger.exception("sandbox cancel failed for %s", call_id)
        for worker in self.workers:
            worker.request_cancel(call_id)
        if self.audit:
            self.audit.record(
                call_id, "tool.cancelling",
                sandbox_killed=killed,
                notified_workers=[w.name for w in self.workers],
            )
        return True

    def is_cancelled(self, call_id: str) -> bool:
        return call_id in self._cancelled

    # ==================================================================
    # Worker 池
    # ==================================================================
    def start_workers(self, count: Optional[int] = None) -> list[Worker]:
        """启动后台 Worker 线程。已启动过则直接复用。"""
        if self.workers:
            return self.workers
        if self.worker_factory is None:
            logger.warning("未配置 worker_factory，异步任务将无人消费")
            return []

        total = count if count is not None else self.config.worker_count
        for index in range(total):
            worker = self.worker_factory(f"worker_{index + 1:02d}")
            worker.start()
            self.workers.append(worker)
        logger.info("started %d workers", len(self.workers))
        return self.workers

    def stop_workers(self) -> None:
        for worker in self.workers:
            worker.stop()
        self.workers = []

    def drain(self, *, max_iterations: int = 5000, workers: int = 1) -> int:
        """把队列里的任务跑完（含到期重试）。演示收尾用它把异步任务收干净。

        与 :meth:`start_workers` 的区别：**不启动后台线程**，而是用当前线程一步步推。

        为什么需要这样一条路径：后台 Worker 一旦启动就会立刻开始消费队列，
        于是「提交 -> 观察状态 -> 再推进」这种分步演示会失去确定性 ——
        你以为在验证 A，实际 B 已经悄悄跑完了。演示与排障需要「一次只动一格」的控制权。

        没有 Worker 时会临时建一个（不 start），所以可以完全不碰线程模型。
        """
        pool = list(self.workers)
        if not pool:
            if self.worker_factory is None:
                return 0
            pool = [self.worker_factory(f"drain_{i + 1:02d}") for i in range(max(1, workers))]

        processed = 0
        for _ in range(max_iterations):
            moved = 0
            for worker in pool:
                if worker.run_once():
                    moved += 1
                    processed += 1
                if processed >= max_iterations:
                    break

            if moved == 0:
                # 队列空了，但可能还有退避中的重试任务。给延迟队列一次提升机会，
                # 提升后仍无任务才判定「真的跑完了」。
                if self.async_executor is not None and self.async_executor.delayed_depth() > 0:
                    self.async_executor.promote_delayed()
                    if any(w.run_once() for w in pool):
                        continue
                break
        return processed

    def worker_stats(self) -> list[dict[str, Any]]:
        return [w.describe() for w in self.workers]

    def queue_stats(self) -> dict[str, Any]:
        if self.async_executor is None:
            return {}
        return self.async_executor.stats()
