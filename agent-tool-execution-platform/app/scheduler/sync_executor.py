"""同步执行器 —— 短任务的直接路径（说明书 §4.1）。

职责分层（刻意的两段式）：

:class:`Executor`
    **执行一次** Tool 的完整动作：抢租约 → 开沙箱 → 注入上下文 → 跑 Tool → 落结果 →
    释放租约。它不做重试决策 —— 它只负责「这一次发生了什么」，并把错误如实分类上报。

:class:`SyncExecutor`
    在 :class:`Executor` 之上套 **§35 Recovery Policy + §36 指数退避**的重试循环。
    它不碰沙箱、不碰结果存储 —— 只根据错误分类决定「再来一次 / 换 Tool / 转人工 / 放弃」。

这样拆开的好处是 **async 路径可以原样复用 Executor**（见 :mod:`app.scheduler.worker`），
重试语义在同步与异步两条路径上完全一致 —— 不会出现「同步会重试、异步忘了重试」这种偏差。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import AppConfig
from ..domain.enums import ErrorType, ExecutionMode, ExecutionStatus, RecoveryAction
from ..domain.errors import Cancelled, ToolPlatformError
from ..domain.models import (
    ExecutionEvent,
    ExecutionRecord,
    SubmitResult,
    ToolCall,
    ToolMetadata,
    utcnow,
)
from ..domain.policy import SandboxPolicy
from ..infra.clock import Clock, SystemClock
from ..recovery.classifier import ErrorClassifier
from ..recovery.policy import RecoveryDecision, RecoveryPolicyEngine
from ..recovery.retry import RetryController
from ..tools.base import ToolContext, ToolOutput
from ..tools.registry import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

ProgressHook = Callable[[str, float, str], None]


@dataclass
class ExecutionOutcome:
    """一次 Tool 执行（可能含多次 attempt）的最终结论。"""

    ok: bool
    call_id: str
    tool_name: str

    result: Any = None                       # ResultEnvelope
    record: Any = None                       # ToolResultRecord
    error_type: Optional[ErrorType] = None
    error_message: Optional[str] = None
    status: ExecutionStatus = ExecutionStatus.FAILED

    attempts: int = 0
    duration_ms: int = 0
    events: list[ExecutionEvent] = field(default_factory=list)

    decision: Optional[RecoveryDecision] = None
    fallback_used: Optional[str] = None
    escalated_to_human: bool = False
    cancelled: bool = False

    detail: dict = field(default_factory=dict)

    @property
    def result_id(self) -> Optional[str]:
        """对外结果标识符，形如 ``result_12``。

        **不要直接用 ``record.id``**：那是数据库 rowid（int），
        而对外契约里 ``result_id`` 是 ``Optional[str]``。把内部主键直接当成
        外部 API 字段暴露，等于把自己的表结构钉进对外契约里。
        """
        raw = getattr(self.record, "id", None)
        return f"result_{raw}" if raw is not None else None

    def to_submit_result(self, *, mode: ExecutionMode) -> SubmitResult:
        """折算成 Gateway 对外返回的 :class:`SubmitResult`。"""
        return SubmitResult(
            call_id=self.call_id,
            status=self.status,
            outcome=self.detail.get("outcome", "FAILED" if not self.ok else "EXECUTED"),
            execution_mode=mode,
            result_id=self.result_id,
            result=self.result,
            error_type=self.error_type.value if self.error_type else None,
            error_message=self.error_message,
            attempt=self.attempts,
            duration_ms=self.duration_ms,
            resume_hint=self.decision.action if self.decision else None,
            detail=dict(self.detail),
        )


class Executor:
    """执行**一次** Tool（不含重试）。同步与异步路径共用。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        config: AppConfig,
        sandbox_manager: Any,
        result_store: Any,
        lease_manager: Any,
        audit: Any = None,
        metrics: Any = None,
        tracer: Any = None,
        clock: Optional[Clock] = None,
        classifier: Optional[ErrorClassifier] = None,
        on_progress: Optional[ProgressHook] = None,
        cancel_check: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.sandbox = sandbox_manager
        self.results = result_store
        self.leases = lease_manager
        self.audit = audit
        self.metrics = metrics
        self.tracer = tracer
        self.clock = clock or SystemClock()
        self.classifier = classifier or ErrorClassifier()
        self.on_progress = on_progress
        self.cancel_check = cancel_check

    # ==================================================================
    def execute(
        self,
        call: ToolCall,
        spec: ToolSpec,
        *,
        worker_id: str = "sync",
        idempotency_key: str = "",
        lease: Any = None,
    ) -> ExecutionOutcome:
        """跑一次 Tool：租约 → 沙箱 → 执行 → 落结果。

        无论成功失败都会**释放租约并销毁沙箱** —— 用 ``finally`` 保证，
        因为沙箱泄漏比执行失败更糟（会一直占着 CPU/内存配额）。

        :param lease: 已持有的租约。Worker 路径会**自己先抢租约再传进来**，
            因为心跳线程需要 ``lease_id`` 才能做 compare-and-renew（§15）；
            同步路径留空由本方法代抢。
        """
        started = self.clock.time()
        outcome = ExecutionOutcome(
            ok=False, call_id=call.call_id, tool_name=call.tool_name
        )

        spec_meta: ToolMetadata = spec.metadata
        call.ensure_idempotency_key()

        # ---- §14 租约：证明「这个 call 现在有主」 ----
        #
        # 同步路径也抢租约，是为了让「调用方进程突然死亡」与「Worker 崩溃」
        # 走**同一套**接管逻辑（§56）—— 否则同步调用挂掉时没有任何痕迹可用于恢复。
        if lease is None:
            lease = self.leases.acquire(
                call.call_id,
                worker_id=worker_id,
                ttl_seconds=self._lease_ttl_for(spec_meta),
            )
        handle = None
        span = None
        try:
            if lease is not None and self.audit:
                self.audit.record(
                    call.call_id, "lease.acquired",
                    worker_id=worker_id, lease_id=lease.lease_id,
                    ttl_seconds=lease.ttl_seconds,
                )

            if self.tracer is not None:
                span = self.tracer.start_span(
                    "tool.execute", tool=call.tool_name, call_id=call.call_id
                )

            if self.audit:
                self.audit.record(
                    call.call_id, "tool.started",
                    tool_name=call.tool_name, attempt=call.attempt,
                    arguments=call.arguments,
                )

            # ---- §28 沙箱：Create Runtime -> Inject Input -> Execute ----
            sandbox_policy = self._sandbox_policy(spec)
            handle = self.sandbox.open(
                call_id=call.call_id,
                policy=sandbox_policy,
                tool_name=call.tool_name,
                timeout_ms=spec_meta.timeout_ms,
            )
            self.sandbox.track(call.call_id, handle)

            if self.audit:
                self.audit.record(
                    call.call_id, "sandbox.created",
                    backend=handle.backend, sandbox_id=handle.sandbox_id,
                    workspace=handle.workspace,
                )

            # ---- 构造 ToolContext：Tool 与平台的唯一接触面 ----
            ctx = ToolContext(
                call=call,
                metadata=spec_meta,
                workspace=handle.workspace,
                run_in_sandbox=self._bind_runner(handle),
                clock=self.clock,
                logger=logging.getLogger(f"tool.{call.tool_name}"),
                progress=self._make_progress(call.call_id),
                started_at=started,
            )

            args = spec.args_model.model_validate(call.arguments)
            output = self._run_tool(spec, args, ctx, call)

            # ---- §37-40 结果处理 + §45 事务化持久化 ----
            #
            # 事件先按原始产物的大小预判，真正的 result_type 要等 ResultProcessor
            # 判完尺寸才知道，所以补一条 result.persisted 事件声明最终形态。
            pre_size = output.size_bytes()
            record, envelope = self.results.persist_success(
                call_id=call.call_id,
                output=output,
                events=[self._event(call.call_id, "tool.completed", {
                    "tool_name": call.tool_name,
                    "output_size_bytes": pre_size,
                })],
            )

            outcome.ok = True
            outcome.status = ExecutionStatus.SUCCESS
            outcome.result = envelope
            outcome.record = record
            outcome.detail["outcome"] = "EXECUTED"
            self._incr("tool_success_total", tool=call.tool_name)
            if self.audit:
                self.audit.record(
                    call.call_id, "result.persisted",
                    result_type=getattr(envelope, "result_type", "inline"),
                    size_bytes=getattr(envelope, "size_bytes", pre_size),
                )

        except BaseException as exc:  # noqa: BLE001 - 统一分类后上报
            classified = self.classifier.wrap(exc)
            outcome.ok = False
            outcome.error_type = classified.error_type
            outcome.error_message = str(classified.message)
            outcome.status = (
                ExecutionStatus.FAILED
                if not isinstance(exc, Cancelled)
                else ExecutionStatus.CANCELLED
            )
            outcome.cancelled = isinstance(exc, Cancelled)
            outcome.detail["error_detail"] = classified.detail
            outcome.events.append(
                self._event(call.call_id, "tool.failed", {
                    "tool_name": call.tool_name,
                    "error_type": classified.error_type.value,
                    "message": classified.message,
                })
            )
            self._incr("tool_failure_total", tool=call.tool_name)
            if classified.error_type == ErrorType.TIMEOUT:
                self._incr("tool_timeout_total", tool=call.tool_name)

        finally:
            # ---- §28 流程收尾：Collect Output -> Destroy ----
            if handle is not None:
                try:
                    self.sandbox.close(handle)
                except Exception:  # noqa: BLE001 - 销毁失败不能掩盖真实错误
                    logger.debug("sandbox close failed for %s", call.call_id, exc_info=True)
            if lease is not None:
                self.leases.release(call.call_id, lease_id=lease.lease_id)

            outcome.attempts = call.attempt + 1
            outcome.duration_ms = int((self.clock.time() - started) * 1000)
            self._observe("tool_execution_ms", outcome.duration_ms, tool=call.tool_name)

            if self.tracer is not None and span is not None:
                self.tracer.end_span(
                    span,
                    status="OK" if outcome.ok else "ERROR",
                    duration_ms=outcome.duration_ms,
                )

        if not outcome.ok:
            # 失败态交给 ResultStore 走 §45 的「状态 + 审计 + Outbox 同事务」
            try:
                self.results.persist_failure(
                    call_id=call.call_id,
                    error_type=outcome.error_type.value if outcome.error_type else "internal_error",
                    error_message=outcome.error_message or "",
                    status=outcome.status,
                    events=outcome.events,
                )
            except Exception:  # noqa: BLE001
                logger.exception("persist_failure failed for %s", call.call_id)

        return outcome

    # ==================================================================
    def _run_tool(self, spec: ToolSpec, args: Any, ctx: ToolContext, call: ToolCall) -> ToolOutput:
        """调用 Tool 本体，并把「取消」检查插在前后。

        取消检查放在这里而不是 Tool 内部：Tool 作者不需要关心取消语义，
        平台在边界上统一处理（§10 的优雅中止思路）。
        """
        if self.cancel_check is not None and self.cancel_check(call.call_id):
            raise Cancelled(f"call {call.call_id} 已被取消")
        output = spec.tool.run(args, ctx)
        if output is None:
            output = ToolOutput(data={"status": "success"})
        return output

    def _bind_runner(self, handle: Any) -> Callable[..., Any]:
        """把沙箱句柄的 ``run`` 绑成 ToolContext 期望的签名。"""

        def _run(argv, *, cwd=None, stdin="", timeout_seconds=None):
            return handle.run(
                argv, cwd=cwd or handle.workspace, stdin=stdin, timeout_seconds=timeout_seconds
            )

        return _run

    def _make_progress(self, call_id: str) -> Callable[[float, str], None]:
        """把 Tool 的进度上报转成审计事件 + 外部钩子（§50 Tool Callback 的雏形）。"""

        def _progress(percent: float, message: str = "") -> None:
            if self.audit:
                self.audit.record(
                    call_id, "tool.progress", percent=round(float(percent), 2), message=message
                )
            if self.on_progress is not None:
                self.on_progress(call_id, float(percent), message)

        return _progress

    def _sandbox_policy(self, spec: ToolSpec) -> SandboxPolicy:
        """沙箱超时兜底：取 Tool 超时与沙箱策略里更宽的那个。

        §29 的三层超时要求 ``Tool Timeout < Sandbox Timeout``：
        让 Tool 自己先超时并给出可读的错误，沙箱只是最后一道保险。
        所以这里取 ``max``，并在配置非法时记 warning（由 ``validate_timeout_layers`` 提前发现）。
        """
        policy = spec.sandbox
        tool_timeout_s = spec.metadata.timeout_ms / 1000.0
        if policy.timeout_seconds < tool_timeout_s:
            logger.warning(
                "Tool %s 的 timeout(%.1fs) 大于沙箱策略 timeout(%.1fs)，已抬升沙箱超时以维持 §29 的三层顺序",
                spec.name, tool_timeout_s, policy.timeout_seconds,
            )
            policy = policy.model_copy(update={"timeout_seconds": tool_timeout_s + 10})
        return policy

    def _lease_ttl_for(self, metadata: ToolMetadata) -> int:
        """§29 第三层：租约必须比沙箱超时还长。

        ``Lease = 330s`` 对应 ``Tool = 300s`` / ``Sandbox = 310s``。
        反过来配会出现「Tool 还在跑、Lease 已过期」→ 别的 Worker 以为它死了并接管 →
        同一份副作用被执行两次。
        """
        tool_timeout_s = metadata.timeout_ms / 1000.0
        return int(max(self.config.lease_ttl_seconds, tool_timeout_s + 30))

    @staticmethod
    def _event(call_id: str, event_type: str, payload: dict) -> ExecutionEvent:
        return ExecutionEvent(call_id=call_id, event_type=event_type, payload=payload)

    def _incr(self, name: str, **labels: str) -> None:
        if self.metrics is not None:
            self.metrics.incr(name, **labels)

    def _observe(self, name: str, value: float, **labels: str) -> None:
        if self.metrics is not None:
            self.metrics.observe(name, value, **labels)


class SyncExecutor:
    """同步执行器：在 :class:`Executor` 外套重试循环（§4.1 / §35 / §36）。"""

    def __init__(
        self,
        executor: Executor,
        *,
        recovery: RecoveryPolicyEngine,
        retry: RetryController,
        registry: ToolRegistry,
        config: AppConfig,
        audit: Any = None,
        metrics: Any = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.executor = executor
        self.recovery = recovery
        self.retry = retry
        self.registry = registry
        self.config = config
        self.audit = audit
        self.metrics = metrics
        self.clock = clock or SystemClock()

    def run(self, call: ToolCall, spec: ToolSpec, *, idempotency_key: str = "") -> ExecutionOutcome:
        """带恢复策略地执行；返回最后一次的结论（成功或最终失败）。"""
        policy = spec.retry
        attempt = 0
        current_spec = spec
        fallback_used: Optional[str] = None
        last: Optional[ExecutionOutcome] = None

        while True:
            call.attempt = attempt
            outcome = self.executor.execute(
                call, current_spec, worker_id="sync", idempotency_key=idempotency_key
            )
            last = outcome

            if outcome.ok:
                outcome.attempts = attempt + 1
                outcome.fallback_used = fallback_used
                outcome.detail["outcome"] = "EXECUTED"
                return outcome

            decision = self.recovery.decide(
                error_type=outcome.error_type or ErrorType.INTERNAL_ERROR,
                tool_name=current_spec.name,
                attempt=attempt,
                risk_level=current_spec.metadata.risk_level,
            )
            outcome.decision = decision
            outcome.attempts = attempt + 1

            if self.audit:
                self.audit.record(
                    call.call_id, "recovery.decided",
                    error_type=decision.error_type.value,
                    action=decision.action.value,
                    attempt=decision.attempt,
                    reason=decision.reason,
                )

            if decision.action == RecoveryAction.RETRY:
                self._incr("tool_retry_total", tool=current_spec.name)
                if self.audit:
                    self.audit.record(
                        call.call_id, "recovery.retry",
                        attempt=attempt + 1, backoff_ms=decision.backoff_ms,
                    )
                self._sleep_ms(decision.backoff_ms)
                attempt += 1
                continue

            if decision.action == RecoveryAction.FALLBACK and decision.fallback_tool:
                fallback_name = decision.fallback_tool
                if not self.registry.has(fallback_name):
                    outcome.error_message = (
                        f"{outcome.error_message}（配置的 fallback_tool={fallback_name} 未注册）"
                    )
                    outcome.detail["outcome"] = "FAILED"
                    outcome.status = ExecutionStatus.FAILED
                    return outcome

                # 换 Tool 后**参数要重新校验**：等价 Tool 的参数模型未必相同。
                current_spec = self.registry.get(fallback_name)
                fallback_used = fallback_name
                attempt += 1
                if self.audit:
                    self.audit.record(
                        call.call_id, "recovery.fallback",
                        from_tool=spec.name, to_tool=fallback_name,
                    )
                if attempt >= self.config.default_retry.max_attempts + 1:
                    outcome.detail["outcome"] = "FAILED"
                    return outcome
                continue

            if decision.action == RecoveryAction.HUMAN:
                outcome.escalated_to_human = True
                outcome.status = ExecutionStatus.WAITING_HUMAN
                outcome.detail["outcome"] = "WAITING_HUMAN"
                self._incr("tool_human_intervention_total", tool=current_spec.name)
                return outcome

            if decision.action == RecoveryAction.WAIT:
                # 同步路径不该出现 WAIT（WAIT 属于幂等 PROCESSING 分支的裁决）。
                # 真出现了说明裁决链有 bug，明确暴露而不是静默当成失败。
                outcome.detail["outcome"] = "WAITING"
                outcome.detail["wait_reason"] = decision.reason
                return outcome

            # REPAIR / ABORT
            outcome.detail["outcome"] = "FAILED"
            outcome.status = ExecutionStatus.FAILED
            return outcome

    # ------------------------------------------------------------------
    def _sleep_ms(self, milliseconds: int) -> None:
        """退避等待。用 clock 而非 time.sleep，让演示里的等待可以被「拨快」。"""
        if milliseconds <= 0:
            return
        self.clock.sleep(milliseconds / 1000.0)

    def _incr(self, name: str, **labels: str) -> None:
        if self.metrics is not None:
            self.metrics.incr(name, **labels)
