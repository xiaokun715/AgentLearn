"""Worker —— 异步任务的实际执行者（说明书 §56 / §62 / §64）。

一个 Worker 的循环做四件事，顺序不能变：

1. **提升延迟任务** —— 把到期的重试任务从 delayed zset 挪回主队列（§36 退避的落点）
2. **认领滞留任务** —— ``XAUTOCLAIM`` 捞回「前任 Worker 投递了但没 ACK」的消息（§56）
3. **取一条新任务** —— ``XREADGROUP >``
4. **执行并 ACK** —— 抢租约 → 起心跳 → 跑 Tool → 落结果 → 更新幂等 → ``XACK``

第 2 步是 Worker 崩溃恢复的关键。**只有 ACK 之后消息才离开 PEL**，
所以 Worker 在「取到任务」和「执行完」之间死掉时，任务不会消失 ——
它躺在 PEL 里等着被 ``XAUTOCLAIM`` 捞走。

但「捞回来」不等于「可以重跑」（§56 最重要的一段）：Worker A 可能并没有死，
只是网络断开。所以这里**不做无条件重跑**，而是回到 :mod:`app.recovery.policy`
按 Tool 的幂等性等级与风险等级分流 —— 可安全重跑的重跑，不可的转人工。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..config import AppConfig
from ..domain.enums import ErrorType, ExecutionStatus, RecoveryAction, RiskLevel
from ..domain.models import ExecutionEvent, ToolCall
from ..infra.clock import Clock, SystemClock
from ..infra.redis import JOB_GROUP, JOB_STREAM, RedisSim
from ..recovery.policy import RecoveryPolicyEngine
from ..recovery.retry import RetryController
from ..tools.registry import ToolRegistry
from .async_executor import AsyncExecutor, JobPayload
from .sync_executor import ExecutionOutcome, Executor

logger = logging.getLogger(__name__)


@dataclass
class WorkerStats:
    """Worker 的运行计数，供观测与演示打印。"""

    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    reclaimed: int = 0
    promoted: int = 0
    requeued_to_human: int = 0
    cancelled: int = 0


class Worker:
    """一个具名 Worker（消费组里的一个 consumer）。"""

    def __init__(
        self,
        *,
        name: str,
        redis: RedisSim,
        config: AppConfig,
        registry: ToolRegistry,
        executor: Executor,
        recovery: RecoveryPolicyEngine,
        retry: RetryController,
        idempotency: Any = None,
        async_executor: Optional[AsyncExecutor] = None,
        lease_manager: Any = None,
        heartbeat_factory: Optional[Callable[..., Any]] = None,
        audit: Any = None,
        metrics: Any = None,
        approvals: Any = None,
        clock: Optional[Clock] = None,
        on_result: Optional[Callable[[ExecutionOutcome], None]] = None,
    ) -> None:
        self.name = name
        self.redis = redis
        self.config = config
        self.registry = registry
        self.executor = executor
        self.recovery = recovery
        self.retry = retry
        self.idempotency = idempotency
        self.async_executor = async_executor
        self.leases = lease_manager
        self.heartbeat_factory = heartbeat_factory
        self.audit = audit
        self.metrics = metrics
        self.approvals = approvals
        self.clock = clock or SystemClock()
        self.on_result = on_result

        self.group = JOB_GROUP
        self.stream = JOB_STREAM
        self.stats = WorkerStats()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cancelled_calls: set[str] = set()
        self._cancel_lock = threading.Lock()

    # ==================================================================
    # 取消（§41 PROCESSING -> CANCELLING -> CANCELLED）
    # ==================================================================
    def request_cancel(self, call_id: str) -> None:
        """登记一个待取消的 call。执行中的会被沙箱层杀掉，未执行的会被跳过。"""
        with self._cancel_lock:
            self._cancelled_calls.add(call_id)

    def is_cancelled(self, call_id: str) -> bool:
        with self._cancel_lock:
            return call_id in self._cancelled_calls

    def _clear_cancel(self, call_id: str) -> None:
        with self._cancel_lock:
            self._cancelled_calls.discard(call_id)

    # ==================================================================
    # 主循环
    # ==================================================================
    def run_once(self, *, promote: bool = True, reclaim: bool = True) -> bool:
        """处理至多一条任务。返回 ``True`` 表示确实处理了一条。"""
        if promote:
            self.stats.promoted += self._promote_delayed()
        if reclaim:
            self.stats.reclaimed += self._reclaim_stale()

        message = self._read_one()
        if message is None:
            return False

        msg_id, payload = message
        try:
            self._process(msg_id, payload)
        except Exception:  # noqa: BLE001 - Worker 绝不能因为单条任务崩溃而退出
            logger.exception("worker %s failed processing %s", self.name, msg_id)
        return True

    def run_until_idle(self, *, max_iterations: int = 1000, idle_rounds: int = 3) -> int:
        """跑到队列空为止（演示/测试最常用）。返回处理条数。

        ``idle_rounds`` 是「连续空转多少轮才算真的没活了」——
        因为一次 ``run_once`` 只取一条，而重试任务会被放进延迟队列，
        需要多轮提升才可能被看到。给几次机会避免过早判定「队列已空」。
        """
        processed = 0
        idle = 0
        for _ in range(max_iterations):
            if self.run_once():
                processed += 1
                idle = 0
            else:
                idle += 1
                if idle >= idle_rounds:
                    break
        return processed

    def run_forever(self, *, poll_interval: Optional[float] = None) -> None:
        """持续消费，直到 :meth:`stop` 被调用。"""
        interval = poll_interval if poll_interval is not None else self.config.worker_poll_interval_seconds
        self._stop.clear()
        while not self._stop.is_set():
            if not self.run_once():
                time.sleep(interval)

    # ==================================================================
    # 后台线程包装
    # ==================================================================
    def start(self, *, poll_interval: Optional[float] = None) -> "Worker":
        """在后台线程里跑 :meth:`run_forever`。"""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever,
            kwargs={"poll_interval": poll_interval},
            name=f"worker-{self.name}",
            daemon=True,
        )
        self._thread.start()
        logger.info("worker %s started", self.name)
        return self

    def stop(self, *, timeout: float = 5.0) -> None:
        """停止后台线程（幂等）。"""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ==================================================================
    # 单条任务的完整处理
    # ==================================================================
    def _process(self, msg_id: str, payload: JobPayload) -> None:
        call = payload.to_call()
        call.ensure_idempotency_key()
        key = call.idempotency_key

        self.stats.processed += 1
        self._incr("tool_call_total", tool=call.tool_name)

        # ---- 取消检查：任务还没开始跑，直接丢弃（§41 CANCELLING） ----
        if self.is_cancelled(call.call_id):
            self.stats.cancelled += 1
            self._clear_cancel(call.call_id)
            if self.audit:
                self.audit.record(call.call_id, "tool.cancelled", reason="已取消，跳过执行")
            self._ack(msg_id)
            return

        if not self.registry.has(call.tool_name):
            # Tool 不存在：ACK 掉，否则会永远卡在 PEL 里被反复认领（毒丸消息）
            self._fail_without_execution(
                call, msg_id, ErrorType.TOOL_NOT_FOUND, f"Tool 未注册: {call.tool_name}"
            )
            return

        spec = self.registry.get(call.tool_name)

        # ---- §14 先抢租约：心跳线程必须知道 lease_id 才能续租（§15） ----
        lease = self.leases.acquire(
            call.call_id,
            worker_id=self.name,
            ttl_seconds=self.executor._lease_ttl_for(spec.metadata),
        )
        if lease is None:
            # 已有别人持租约在跑 —— 这条是重复投递，让出去而不是硬抢
            if self.audit:
                self.audit.record(
                    call.call_id, "lease.rejected",
                    worker_id=self.name, reason="租约已被占用，说明任务在别处执行中",
                )
            self._ack(msg_id)
            return

        # ---- 幂等执行权：确认这次执行确实归本 Worker 所有 ----
        #
        # 常态下 Gateway 已经认领过幂等键，这里只是把 worker_id / lease_id 回填上去
        # （CAS：只有 PROCESSING 能改）。
        #
        # 但**回收路径**不是常态：Reaper 判定上一个 Worker 已死时，会把执行权归还
        # （`release_claim` 删掉记录），好让接管者能重新 SET NX。如果这里不补认领，
        # 后面 `complete_success` 就会去迁移一条**已经不存在的记录** ——
        # 结果是：执行照常成功，但幂等键消失了，下一次相同调用会被当成全新请求
        # 再跑一遍。这恰恰是幂等机制要防的事，而且发生在**恢复路径**上，
        # 也就是系统本来就已经在故障中的时候。
        if self.idempotency is not None:
            claim = self.idempotency.store.ensure_owned(
                key=key,
                call_id=call.call_id,
                worker_id=self.name,
                lease_id=lease.lease_id,
            )
            if not claim.acquired:
                # 记录是 SUCCESS —— 已经真的做完了，这是重复投递，绝不能重跑（§12）
                if self.audit:
                    self.audit.record(
                        call.call_id, "idempotency.skip",
                        worker_id=self.name,
                        status=getattr(claim.record, "status", "unknown"),
                        reason="幂等已 SUCCESS，本条为重复投递，跳过执行",
                    )
                self.leases.release(call.call_id, lease_id=lease.lease_id)
                self._ack(msg_id)
                return

        heartbeat = None
        if self.heartbeat_factory is not None:
            try:
                heartbeat = self.heartbeat_factory(
                    call.call_id, lease_id=lease.lease_id, worker_id=self.name
                )
                heartbeat.start()
            except Exception:  # noqa: BLE001 - 心跳起不来不该阻断执行
                logger.exception("failed to start heartbeat for %s", call.call_id)
                heartbeat = None

        try:
            outcome = self.executor.execute(
                call, spec, worker_id=self.name, idempotency_key=key, lease=lease
            )
        finally:
            if heartbeat is not None:
                heartbeat.stop()
            self._clear_cancel(call.call_id)

        self._finalize(msg_id, call, spec, outcome, payload)
        if self.on_result is not None:
            try:
                self.on_result(outcome)
            except Exception:  # noqa: BLE001
                logger.exception("on_result hook failed")

    # ------------------------------------------------------------------
    def _finalize(self, msg_id: str, call: ToolCall, spec: Any,
                  outcome: ExecutionOutcome, payload: JobPayload) -> None:
        """按执行结论更新幂等状态、决定是否重试、最后 ACK。"""
        key = call.idempotency_key

        if outcome.ok:
            self.stats.succeeded += 1
            if self.idempotency is not None:
                self.idempotency.complete_success(
                    key, call_id=call.call_id, result_id=outcome.result_id,
                )
            self._ack(msg_id)
            return

        self.stats.failed += 1
        error_type = outcome.error_type or ErrorType.INTERNAL_ERROR

        # 取消导致的失败：按 CANCELLED 收尾，不进恢复策略
        if outcome.cancelled:
            self.stats.cancelled += 1
            if self.idempotency is not None:
                self.idempotency.complete_failure(
                    key, call_id=call.call_id, error="cancelled",
                    error_type=ErrorType.INTERNAL_ERROR,
                )
            self._ack(msg_id)
            return

        # ---- §35 恢复策略：重试 / 转人工 / 放弃 ----
        decision = self.recovery.decide(
            error_type=error_type,
            tool_name=spec.name,
            attempt=payload.attempt,
            risk_level=spec.metadata.risk_level,
        )
        outcome.decision = decision

        if decision.action == RecoveryAction.RETRY and self.async_executor is not None:
            self.stats.retried += 1
            if self.idempotency is not None:
                # 重试要先把执行权**归还**，否则下一轮 try_claim 会看到 PROCESSING 而让路
                self.idempotency.complete_failure(
                    key, call_id=call.call_id,
                    error=outcome.error_message or "", error_type=error_type,
                )
                self.idempotency.store.release_claim(key)
            if self.audit:
                self.audit.record(
                    call.call_id, "recovery.retry",
                    attempt=payload.attempt + 1, backoff_ms=decision.backoff_ms,
                    reason=decision.reason,
                )
            self._incr("tool_retry_total", tool=spec.name)
            self.async_executor.retry_later(payload, delay_ms=decision.backoff_ms)
            self._ack(msg_id)
            return

        if decision.action == RecoveryAction.HUMAN:
            self.stats.requeued_to_human += 1
            self._escalate_to_human(call, spec, outcome, decision)
            self._ack(msg_id)
            return

        if decision.action == RecoveryAction.FALLBACK and decision.fallback_tool:
            if self.async_executor is not None and self.registry.has(decision.fallback_tool):
                if self.audit:
                    self.audit.record(
                        call.call_id, "recovery.fallback",
                        from_tool=spec.name, to_tool=decision.fallback_tool,
                    )
                fallback_payload = JobPayload.from_call(
                    call.model_copy(update={"tool_name": decision.fallback_tool}),
                    risk_level=payload.risk_level,
                    timeout_ms=payload.timeout_ms,
                )
                fallback_payload.attempt = payload.attempt
                self.async_executor.retry_later(fallback_payload, delay_ms=0)
                self._ack(msg_id)
                return

        # ABORT / REPAIR / 无 fallback
        if self.idempotency is not None:
            self.idempotency.complete_failure(
                key, call_id=call.call_id,
                error=outcome.error_message or "", error_type=error_type,
            )
        self._ack(msg_id)

    def _escalate_to_human(self, call: ToolCall, spec: Any,
                           outcome: ExecutionOutcome, decision: Any) -> None:
        """§51/§56：不可自动恢复的场景转人工，并把执行状态置为 WAITING_HUMAN。"""
        if self.approvals is not None:
            self.approvals.request(
                call,
                risk_level=spec.metadata.risk_level,
                reason=decision.reason,
            )
        if self.idempotency is not None:
            self.idempotency.complete_failure(
                call.idempotency_key, call_id=call.call_id,
                error=decision.reason,
                error_type=outcome.error_type or ErrorType.INTERNAL_ERROR,
            )
        if self.audit:
            self.audit.record(
                call.call_id, "recovery.human",
                reason=decision.reason, action=decision.action.value,
            )
        self._incr("tool_human_intervention_total", tool=spec.name)

    def _fail_without_execution(self, call: ToolCall, msg_id: str,
                                error_type: ErrorType, message: str) -> None:
        """连执行都没开始的失败（如 Tool 未注册）。必须 ACK，避免毒丸反复投递。"""
        self.stats.failed += 1
        if self.idempotency is not None:
            self.idempotency.complete_failure(
                call.idempotency_key, call_id=call.call_id,
                error=message, error_type=error_type,
            )
        if self.audit:
            self.audit.record(
                call.call_id, "tool.failed",
                error_type=error_type.value, message=message, stage="pre_execution",
            )
        self._incr("tool_failure_total", tool=call.tool_name)
        self._ack(msg_id)

    # ==================================================================
    # 队列协作
    # ==================================================================
    def _read_one(self) -> Optional[tuple[str, JobPayload]]:
        """读一条新消息（``>`` 语义），并把 ``payload`` 字段解析出来。"""
        entries = self.redis.xreadgroup(
            self.group, self.name, {self.stream: ">"}, count=1
        )
        if not entries:
            return None
        msg_id, fields = entries[0]
        raw = fields.get("payload")
        if not raw:
            self._ack(msg_id)
            return None
        try:
            return msg_id, JobPayload.from_json(raw)
        except Exception:  # noqa: BLE001 - 坏消息直接丢弃并 ACK
            logger.exception("malformed job payload %s", msg_id)
            self._ack(msg_id)
            return None

    def _ack(self, msg_id: str) -> None:
        self.redis.xack(self.stream, self.group, msg_id)

    def _promote_delayed(self) -> int:
        if self.async_executor is None:
            return 0
        return self.async_executor.promote_delayed()

    def _reclaim_stale(self, *, min_idle_ms: float = 30_000) -> int:
        """接管前任 Worker 遗留的未 ACK 消息（§56 的 XAUTOCLAIM）。

        认领到之后**不直接重跑** —— 先看幂等状态：如果其实是别人正在跑（租约还活着），
        就放回去；确认无人持有才重新入队。这样才对得起 §56 那句
        「Worker A 可能实际上没有死，只是网络断开」。
        """
        claimed = self.redis.xautoclaim(
            self.stream, self.group, self.name, min_idle_ms=min_idle_ms, count=10
        )
        count = 0
        for msg_id, fields in claimed:
            raw = fields.get("payload")
            if not raw:
                self._ack(msg_id)
                continue
            try:
                payload = JobPayload.from_json(raw)
            except Exception:  # noqa: BLE001
                self._ack(msg_id)
                continue

            call_id = payload.call_id
            if self.leases is not None and self.leases.is_alive(call_id):
                # 原主人还活着 —— 唯一的正确动作是继续等，不是抢跑（§56）
                if self.audit:
                    self.audit.record(
                        call_id, "lease.reclaim.skipped",
                        worker_id=self.name,
                        reason="租约仍然有效，原 Worker 只是网络抖动，不接管",
                    )
                self._ack(msg_id)
                continue

            if self.audit:
                self.audit.record(
                    call_id, "lease.reclaim",
                    worker_id=self.name,
                    reason="前任 Worker 未 ACK 且租约已过期，接管重试",
                )
            self._incr("tool_lease_expired_total", tool=payload.tool_name)
            self._incr("tool_recovery_total", tool=payload.tool_name)
            if self.async_executor is not None:
                self.async_executor.retry_later(payload, delay_ms=0)
                count += 1
            self._ack(msg_id)
        return count

    # ------------------------------------------------------------------
    def _incr(self, name: str, **labels: str) -> None:
        if self.metrics is not None:
            self.metrics.incr(name, **labels)

    def describe(self) -> dict[str, Any]:
        return {
            "worker": self.name,
            "running": self.running,
            "group": self.group,
            "stream": self.stream,
            "stats": self.stats.__dict__,
        }
