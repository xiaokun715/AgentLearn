"""Tool Gateway —— Tool Call 的唯一入口（说明书 §2 / §54 / §75）。

这是整个平台的门面。Agent 只会做一件事：**把 :class:`ToolCall` 交给 Gateway**；
Gateway 负责把「一次调用」变成「一次安全、幂等、可恢复的执行」。§54 的完整链路在这里落地：

::

    调用方
      │
      ▼
    Tool Gateway
      │
      ├─ ① Registry 查找 ────── Tool 不存在 -> REJECTED（§35 FALLBACK 由恢复策略接手）
      ├─ ② 重复/循环检测 ────── §30 / §31 / §32，可能 DEGRADED 或 STOP
      ├─ ③ Schema 校验 + 参数自愈 ─ §21 / §22 / §23
      ├─ ④ 注入检测 ─────────── §24 / §25
      ├─ ⑤ 权限校验 ─────────── §26（失败直接拒绝，不 retry）
      ├─ ⑥ 风险检查 ─────────── §51 HIGH -> WAITING_HUMAN
      ├─ ⑦ 幂等检查 ─────────── §9~§13 四分支
      │     ├── NOT FOUND  -> 抢执行权 -> 继续
      │     ├── PROCESSING -> 租约活着 WAIT / 租约过期看幂等等级
      │     ├── SUCCESS    -> 直接复用结果，绝不重跑
      │     └── FAILED     -> ErrorClassifier -> Recovery Policy
      └─ ⑧ Scheduler 分流 ───── §4 sync / async
                                   │
                                   ▼
                            Result / job_id

**顺序很重要**，几个刻意的安排：

* **权限在幂等之前**。权限失败不该占用（也不该污染）幂等键 ——
  否则一次越权尝试会让一个合法的同名调用在 TTL 内被误判为「已经失败过」。
* **风险检查在幂等之前**。高风险 Tool 要先拿到人的批准才配占有执行权；
  否则幂等记录会停在 PROCESSING 等一个可能永远不来的人。
* **自愈在注入检测之前**。先把 ``"300"`` 修成 ``300``，再检查值里有没有 ``../``——
  顺序反了会因为类型怪异而漏检。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import AppConfig
from ..domain.enums import (
    ErrorType,
    ExecutionMode,
    ExecutionStatus,
    IdempotencyStatus,
    LoopSignal,
    RecoveryAction,
    RiskLevel,
    SubmitOutcome,
)
from ..domain.errors import (
    HumanRejected,
    InjectionDetected,
    PermissionDenied,
    ToolNotFound,
    ToolPlatformError,
)
from ..domain.models import (
    ExecutionEvent,
    ExecutionRecord,
    PendingToolCall,
    ResultEnvelope,
    SubmitResult,
    ToolCall,
    arguments_hash,
    utcnow,
)
from ..infra.clock import Clock, SystemClock
from ..infra.database import Database
from ..infra.redis import RedisSim
from ..scheduler.scheduler import Scheduler
from ..tools.registry import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)


@dataclass
class GatewayStats:
    """Gateway 层面的计数（与 Metrics 互补：这里是权威来源，Metrics 是可观测导出）。"""

    submitted: int = 0
    rejected: int = 0
    deduplicated: int = 0
    waiting: int = 0
    waiting_human: int = 0
    executed: int = 0
    accepted: int = 0
    degraded: int = 0
    stopped: int = 0


@dataclass
class DuplicateCheck:
    """重复/循环检测的合并结论。"""

    signal: LoopSignal = LoopSignal.OK
    reasons: list[str] = field(default_factory=list)
    degradation: dict = field(default_factory=dict)


class ToolGateway:
    """平台唯一入口。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        config: AppConfig,
        scheduler: Scheduler,
        db: Database,
        redis: RedisSim,
        validator: Any,
        injection: Any,
        permission: Any,
        idempotency: Any,
        lease_manager: Any,
        result_store: Any,
        recovery: Any = None,
        duplicates: Any = None,
        cycles: Any = None,
        approvals: Any = None,
        metrics: Any = None,
        tracer: Any = None,
        audit: Any = None,
        classifier: Any = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.scheduler = scheduler
        self.db = db
        self.redis = redis
        self.validator = validator
        self.injection = injection
        self.permission = permission
        self.idempotency = idempotency
        self.leases = lease_manager
        self.results = result_store
        self.recovery = recovery
        self.duplicates = duplicates
        self.cycles = cycles
        self.approvals = approvals
        self.metrics = metrics
        self.tracer = tracer
        self.audit = audit
        self.classifier = classifier
        self.clock = clock or SystemClock()

        self.stats = GatewayStats()
        # 供 Agent 层注册「Tool 完成」回调（§50 Tool Callback）
        self._completion_hooks: list[Callable[[str, SubmitResult], None]] = []

    # ==================================================================
    # 回调注册（§50）
    # ==================================================================
    def on_completion(self, hook: Callable[[str, SubmitResult], None]) -> None:
        """注册 Tool 完成回调。Worker 侧完成后会通知，用于唤醒 LangGraph（§50）。"""
        self._completion_hooks.append(hook)

    def _emit_completion(self, call_id: str, result: SubmitResult) -> None:
        for hook in self._completion_hooks:
            try:
                hook(call_id, result)
            except Exception:  # noqa: BLE001 - 回调失败不能影响主流程
                logger.exception("completion hook failed for %s", call_id)

    # ==================================================================
    # 提交
    # ==================================================================
    def submit(self, call: ToolCall) -> SubmitResult:
        """提交一次 Tool 调用。这是平台的**唯一入口**。"""
        started = self.clock.time()
        self.stats.submitted += 1
        call.ensure_idempotency_key()
        key = call.idempotency_key

        self._incr("tool_call_total", tool=call.tool_name)
        if self.audit:
            self.audit.record(
                call.call_id, "call.received",
                tool_name=call.tool_name, agent_id=call.agent_id,
                run_id=call.graph_run_id, idempotency_key=key,
                arguments=call.arguments,
            )

        # ---------- ① Registry ----------
        if not self.registry.has(call.tool_name):
            return self._reject(
                call, ErrorType.TOOL_NOT_FOUND,
                f"Tool 未注册: {call.tool_name}",
                detail={"registered_tools": self.registry.names()},
            )
        spec = self.registry.get(call.tool_name)

        # ---------- ② 重复 / 循环检测（§30 ~ §33） ----------
        dup = self._check_loops(call)
        if dup.signal == LoopSignal.STOP:
            self.stats.stopped += 1
            return self._reject(
                call, ErrorType.INTERNAL_ERROR,
                "检测到执行循环，已停止该调用",
                detail={
                    "reasons": dup.reasons,
                    "degradation": dup.degradation,
                    # 键名与其它返回路径保持一致（都叫 loop_signal）：
                    # 同一个语义在同一个结构里有两个名字，调用方就得写两套判断，
                    # 而且**只有被拦下**那条路径才会漏掉信号 —— 最难 debug 的组合。
                    "loop_signal": dup.signal.value,
                },
            )
        if dup.signal in (LoopSignal.DEGRADED, LoopSignal.WARNING):
            self.stats.degraded += 1

        # ---------- ③ Schema 校验 + 参数自愈（§21 ~ §23） ----------
        validation = self.validator.validate(call)
        if validation.repaired and self.audit:
            self.audit.record(
                call.call_id, "parameter.repaired",
                steps=[s.__dict__ for s in validation.repaired],
                repaired_by_llm=validation.repaired_by_llm,
            )
        if not validation.ok:
            return self._reject(
                call, ErrorType.VALIDATION_ERROR,
                "参数校验失败且无法自愈: " + "; ".join(validation.errors),
                detail={"errors": validation.errors, "original": call.arguments},
            )
        # 用自愈后的参数继续 —— 后续幂等键也必须基于**修复后**的参数计算，
        # 否则「'300' 的调用」与「300 的调用」会得到两个不同的幂等键（§7）。
        repaired_arguments = dict(validation.arguments)
        if repaired_arguments != call.arguments:
            call.arguments = repaired_arguments
            call.idempotency_key = ""
            key = call.ensure_idempotency_key()

        # ---------- ④ 注入检测（§24 / §25） ----------
        try:
            if spec.param_policy.injection_guard:
                self.injection.assert_clean(call.arguments, rules=spec.param_policy.rules)
        except InjectionDetected as exc:
            if self.audit:
                self.audit.record(
                    call.call_id, "injection.detected",
                    findings=exc.detail.get("findings", []),
                )
            return self._reject(
                call, ErrorType.VALIDATION_ERROR,
                f"参数注入检测拦截: {exc.message}",
                detail=exc.detail,
            )

        # ---------- ⑤ 权限校验（§26） ----------
        decision = self.permission.check_tool(call.agent_id, spec)
        if not decision.allowed:
            if self.audit:
                self.audit.record(
                    call.call_id, "permission.denied",
                    principal=call.agent_id, missing=decision.missing,
                )
            self._incr("tool_failure_total", tool=call.tool_name)
            return self._reject(
                call, ErrorType.PERMISSION_ERROR,
                f"权限不足：缺少 {decision.missing}（权限失败不重试）",
                detail={"missing": decision.missing, "granted": decision.granted},
            )

        # ---------- ⑥ 风险检查（§51 / §67） ----------
        #
        # 审批已通过则放行 —— 否则 approve() 重建出来的调用会再次撞上这道闸门，
        # 变成「批了还要再批」的死循环。判据是**审批单本身的状态**，
        # 而不是「谁在调」：人可能换了，但那张批准单是对这次 call_id 生效的。
        if spec.metadata.risk_level == RiskLevel.HIGH and not self._is_approved(call.call_id):
            return self._request_human(call, spec, reason="高风险 Tool，需要人工审批")

        # ---------- ⑦ 幂等检查（§9 ~ §13） ----------
        claim = self.idempotency.begin(call)
        if not claim.acquired:
            return self._handle_existing(call, spec, claim.record, key, dup)

        # 记录一条执行记录（§41 CREATED -> VALIDATING -> QUEUED）
        execution = ExecutionRecord(
            call_id=call.call_id,
            run_id=call.graph_run_id,
            agent_id=call.agent_id,
            tenant_id=call.tenant_id,
            tool_name=call.tool_name,
            tool_version=call.tool_version,
            idempotency_key=key,
            arguments=dict(call.arguments),
            arguments_hash=arguments_hash(call.arguments),
            status=ExecutionStatus.QUEUED,
            attempt=call.attempt,
        )
        self.db.upsert_execution(execution)
        self.db.append_event(
            call.call_id, "execution.queued",
            {"tool_name": call.tool_name, "mode": spec.metadata.execution_mode.value},
        )

        # ---------- ⑧ 分流执行（§4） ----------
        try:
            dispatched = self.scheduler.dispatch(call, spec, idempotency_key=key)
        except ToolPlatformError as exc:
            self.idempotency.complete_failure(
                key, call_id=call.call_id,
                error=str(exc), error_type=exc.error_type,
            )
            return self._reject(call, exc.error_type, str(exc), detail=exc.detail)

        if dispatched.accepted:
            # 异步：Agent 拿到 job_id 后应写 Checkpoint 并放手（§49）
            self.stats.accepted += 1
            self.db.update_execution(call.call_id, status=ExecutionStatus.PROCESSING)
            result = SubmitResult(
                call_id=call.call_id,
                status=ExecutionStatus.PROCESSING,
                outcome=SubmitOutcome.ACCEPTED.value,
                execution_mode=ExecutionMode.ASYNC,
                job_id=dispatched.job_id,
                attempt=call.attempt,
                duration_ms=int((self.clock.time() - started) * 1000),
                resume_hint=RecoveryAction.WAIT,
                detail={
                    "degradation": dup.degradation,
                    "loop_signal": dup.signal.value,
                    # §56/§57 的判据：恢复流程（Agent 侧或 Reaper 侧）要据这两项
                    # 决定「租约过期后能否接管重跑」。放在 detail 里随结果一起传，
                    # 免得恢复方为了两个枚举值再回查一次 registry。
                    "risk_level": spec.metadata.risk_level.value,
                    "idempotency_level": spec.metadata.idempotency_level.value,
                    "tool_name": call.tool_name,
                },
            )
            return result

        outcome = dispatched.sync
        assert outcome is not None  # 非异步必为同步结果

        if outcome.ok:
            self.stats.executed += 1
            self.idempotency.complete_success(
                key, call_id=call.call_id, result_id=outcome.result_id,
            )
            result = SubmitResult(
                call_id=call.call_id,
                status=ExecutionStatus.SUCCESS,
                outcome=SubmitOutcome.EXECUTED.value,
                execution_mode=ExecutionMode.SYNC,
                result_id=outcome.result_id,
                result=outcome.result,
                attempt=outcome.attempts,
                duration_ms=outcome.duration_ms,
                detail={
                    "degradation": dup.degradation,
                    "loop_signal": dup.signal.value,
                    "fallback_used": outcome.fallback_used,
                    "risk_level": spec.metadata.risk_level.value,
                    "idempotency_level": spec.metadata.idempotency_level.value,
                    "tool_name": call.tool_name,
                },
            )
            self._emit_completion(call.call_id, result)
            return result

        return self._finalize_failure(call, spec, outcome, key, dup)

    # ==================================================================
    # 幂等命中分支（§11 / §12 / §13）
    # ==================================================================
    def _handle_existing(
        self,
        call: ToolCall,
        spec: ToolSpec,
        record: Any,
        key: str,
        dup: Optional[DuplicateCheck] = None,
    ) -> SubmitResult:
        """幂等已存在时的裁决。

        ``dup`` 会被并进每条返回路径的 ``detail`` —— **重复检测在幂等命中的场景下
        尤其重要**：Agent 反复提交同一个调用时，平台会把后续请求全部幂等复用
        （工具一次都没重跑），从平台指标上看一切正常，但 Agent 其实卡住了。
        只有把 loop_signal 一路带出去，这种「**被幂等掩盖的死循环**」才看得见。
        """
        dup = dup or DuplicateCheck()
        loop_detail = {
            "degradation": dup.degradation,
            "loop_signal": dup.signal.value,
        }

        if record is None:
            # 极罕见：抢执行权失败但记录又读不到（TTL 刚好到期）。当成可重试处理。
            return SubmitResult(
                call_id=call.call_id,
                status=ExecutionStatus.QUEUED,
                outcome=SubmitOutcome.FAILED.value,
                error_type=ErrorType.INTERNAL_ERROR.value,
                error_message="幂等记录竞争后消失，请重试",
                resume_hint=RecoveryAction.RETRY,
                detail=dict(loop_detail),
            )

        # ---- Case 3：SUCCESS -> 直接复用结果，绝不能再次执行（§12） ----
        if record.status == IdempotencyStatus.SUCCESS:
            self.stats.deduplicated += 1
            self._incr("tool_idempotency_hit_total", tool=call.tool_name)
            if self.audit:
                self.audit.record(
                    call.call_id, "idempotency.hit",
                    status="SUCCESS", original_call_id=record.call_id,
                    reason="幂等命中：复用已有结果，不再执行",
                )
            envelope: Optional[ResultEnvelope] = None
            if record.call_id:
                envelope = self.results.load_envelope(record.call_id)
            return SubmitResult(
                call_id=record.call_id or call.call_id,
                status=ExecutionStatus.SUCCESS,
                outcome=SubmitOutcome.DEDUPLICATED.value,
                execution_mode=spec.metadata.execution_mode,
                result_id=record.result_id,
                result=envelope,
                deduplicated=True,
                detail={"reason": "幂等命中 SUCCESS，直接返回缓存结果", **loop_detail},
            )

        # ---- Case 2 / 4：交给幂等管理器按租约与错误类型裁决 ----
        verdict = self.idempotency.decide(
            record,
            lease_manager=self.leases,
            risk_level=spec.metadata.risk_level,
            idempotency_level=spec.metadata.idempotency_level,
            attempt=call.attempt,
            max_attempts=spec.retry.max_attempts,
            config=self.config.recovery,
            claim_grace_seconds=self.config.idempotency_claim_grace_seconds,
        )

        if verdict.action == RecoveryAction.WAIT:
            self.stats.waiting += 1
            self._incr("tool_idempotency_hit_total", tool=call.tool_name)
            if self.audit:
                self.audit.record(
                    call.call_id, "idempotency.hit",
                    status=record.status.value, original_call_id=record.call_id,
                    reason=verdict.reason,
                )
            return SubmitResult(
                call_id=record.call_id or call.call_id,
                status=ExecutionStatus.PROCESSING,
                outcome=SubmitOutcome.WAITING.value,
                execution_mode=spec.metadata.execution_mode,
                attempt=call.attempt,
                resume_hint=RecoveryAction.WAIT,
                detail={"reason": verdict.reason, "lease_alive": True, **loop_detail},
            )

        if verdict.action == RecoveryAction.HUMAN:
            return self._request_human(call, spec, reason=verdict.reason)

        if verdict.action == RecoveryAction.RETRY:
            # 上一轮失败 / 租约过期且 Tool 可安全接管 —— 拿回执行权后**真的重跑**。
            #
            # 两道防无限递归的闸门：
            #   ① attempt 上限（Tool 自己的 retry.max_attempts）
            #   ② 重复检测（§30）—— 若这个 run 已经反复调同一个 Tool，
            #      _check_loops 会把信号抬到 STOP 并在下一轮直接拒绝
            max_total = max(spec.retry.max_attempts, 1) + 1
            if call.attempt + 1 >= max_total:
                return SubmitResult(
                    call_id=record.call_id or call.call_id,
                    status=ExecutionStatus.FAILED,
                    outcome=SubmitOutcome.FAILED.value,
                    execution_mode=spec.metadata.execution_mode,
                    error_type=record.error_type,
                    error_message=(
                        f"{record.error or verdict.reason}"
                        f"（重试次数已用尽 {call.attempt + 1}/{max_total}）"
                    ),
                    attempt=call.attempt,
                    resume_hint=RecoveryAction.ABORT,
                    detail={"reason": "重试次数用尽，停止重试", **loop_detail},
                )

            if self.idempotency.store.release_claim(key):
                call.attempt += 1
                if self.audit:
                    self.audit.record(
                        call.call_id, "idempotency.reclaim",
                        attempt=call.attempt, reason=verdict.reason,
                    )
                self._incr("tool_retry_total", tool=call.tool_name)
                return self.submit(call)

        return SubmitResult(
            call_id=record.call_id or call.call_id,
            status=ExecutionStatus.FAILED,
            outcome=SubmitOutcome.FAILED.value,
            execution_mode=spec.metadata.execution_mode,
            error_type=record.error_type,
            error_message=record.error or verdict.reason,
            resume_hint=verdict.action,
            detail={"reason": verdict.reason, "reclaim": False, **loop_detail},
        )

    # ==================================================================
    # 失败收尾与恢复策略
    # ==================================================================
    def _finalize_failure(
        self, call: ToolCall, spec: ToolSpec, outcome: Any, key: str, dup: DuplicateCheck
    ) -> SubmitResult:
        decision = outcome.decision
        error_type = outcome.error_type or ErrorType.INTERNAL_ERROR

        if outcome.escalated_to_human or (decision and decision.action == RecoveryAction.HUMAN):
            self.idempotency.complete_failure(
                key, call_id=call.call_id,
                error=outcome.error_message or "", error_type=error_type,
            )
            return self._request_human(
                call, spec,
                reason=(decision.reason if decision else "执行失败且不可自动恢复"),
            )

        self.idempotency.complete_failure(
            key, call_id=call.call_id,
            error=outcome.error_message or "", error_type=error_type,
        )
        self._incr("tool_failure_total", tool=call.tool_name)
        if self.metrics is not None and outcome.attempts > 1:
            self._incr("tool_recovery_total", tool=call.tool_name)

        action = decision.action if decision else RecoveryAction.ABORT
        return SubmitResult(
            call_id=call.call_id,
            status=ExecutionStatus.FAILED,
            outcome=SubmitOutcome.FAILED.value,
            execution_mode=spec.metadata.execution_mode,
            error_type=error_type.value,
            error_message=outcome.error_message,
            attempt=outcome.attempts,
            duration_ms=outcome.duration_ms,
            resume_hint=action,
            detail={
                "reason": decision.reason if decision else "无恢复策略可用",
                "fallback_used": outcome.fallback_used,
                "degradation": dup.degradation,
                "loop_signal": dup.signal.value,
            },
        )

    # ==================================================================
    # 人工介入（§51 ~ §53）
    # ==================================================================
    def _request_human(self, call: ToolCall, spec: ToolSpec, *, reason: str) -> SubmitResult:
        self.stats.waiting_human += 1
        record = None
        if self.approvals is not None:
            record = self.approvals.request(
                call, risk_level=spec.metadata.risk_level, reason=reason
            )
        self.db.upsert_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=call.graph_run_id,
                agent_id=call.agent_id,
                tenant_id=call.tenant_id,
                tool_name=call.tool_name,
                tool_version=call.tool_version,
                idempotency_key=call.idempotency_key,
                arguments=dict(call.arguments),
                arguments_hash=arguments_hash(call.arguments),
                status=ExecutionStatus.WAITING_HUMAN,
                attempt=call.attempt,
            )
        )
        self._incr("tool_human_intervention_total", tool=call.tool_name)
        return SubmitResult(
            call_id=call.call_id,
            status=ExecutionStatus.WAITING_HUMAN,
            outcome=SubmitOutcome.WAITING_HUMAN.value,
            execution_mode=spec.metadata.execution_mode,
            resume_hint=RecoveryAction.HUMAN,
            detail={
                "reason": reason,
                "risk_level": spec.metadata.risk_level.value,
                "approval_status": getattr(record, "status", "WAITING_HUMAN"),
            },
        )

    def approve(self, call_id: str, *, reviewer: str = "admin", reason: str = "") -> SubmitResult:
        """§52 ``--APPROVE-->`` 恢复执行。

        审批通过**不等于**免检 —— 重新走一遍 submit，只不过这次会跳过风险检查
        （审批单已是 APPROVED）。权限、幂等、沙箱一样都不能少。
        """
        if self.approvals is None:
            raise RuntimeError("未配置 HumanApprovalManager")
        approval = self.approvals.get(call_id)
        if approval is None:
            raise HumanRejected(f"找不到审批单: {call_id}")
        self.approvals.approve(call_id, reviewer=reviewer, reason=reason)

        # 用审批单**原样重建**调用：身份、run_id、参数都来自单据，
        # 而不是在这里临时拼一个 —— 拼出来的东西一旦和原调用有一处不一致，
        # 审批的意义（「我批的是这一笔」）就没了。
        call = approval.rebuild_call()
        return self.submit(call)

    def reject(self, call_id: str, *, reviewer: str = "admin", reason: str = "") -> SubmitResult:
        """§52 ``--REJECT-->`` 取消执行。"""
        if self.approvals is None:
            raise RuntimeError("未配置 HumanApprovalManager")
        self.approvals.reject(call_id, reviewer=reviewer, reason=reason)
        self.db.update_execution(call_id, status=ExecutionStatus.CANCELLED)
        self.db.append_event(call_id, "human.rejected", {"reviewer": reviewer, "reason": reason})
        return SubmitResult(
            call_id=call_id,
            status=ExecutionStatus.CANCELLED,
            outcome=SubmitOutcome.REJECTED.value,
            error_type=ErrorType.PERMISSION_ERROR.value,
            error_message=f"人工审批驳回: {reason}",
        )

    def modify(
        self, call_id: str, *, arguments: dict, reviewer: str = "admin", reason: str = ""
    ) -> SubmitResult:
        """§52 ``--MODIFY-->`` 修改参数后重新执行。

        **修改后的参数必须重新过一遍完整校验**（§52 的 ``Parameter Repair`` 箭头）——
        人也会写错参数，而且人比 LLM 更有可能被社会工程学说服去放宽约束。
        """
        if self.approvals is None:
            raise RuntimeError("未配置 HumanApprovalManager")
        approval = self.approvals.modify(
            call_id, reviewer=reviewer, arguments=arguments, reason=reason
        )
        # 参数被人改过了 -> 幂等键必须重算（§7：参数变了，键就得变），
        # 所以 rebuild_call 统一把 idempotency_key 置空交给平台重算。
        return self.submit(approval.rebuild_call(arguments=arguments))

    # ==================================================================
    # 查询（§58 GET /v1/tool-calls/{call_id}）
    # ==================================================================
    def query(self, call_id: str) -> SubmitResult:
        """按 call_id 查当前状态。Agent 崩溃恢复与轮询都走这里（§19 / §55）。"""
        record = self.db.get_execution(call_id)
        if record is None:
            return SubmitResult(
                call_id=call_id,
                status=ExecutionStatus.CREATED,
                outcome=SubmitOutcome.FAILED.value,
                error_type=ErrorType.TOOL_NOT_FOUND.value,
                error_message=f"未找到执行记录: {call_id}",
                resume_hint=RecoveryAction.RETRY,
            )

        result: Optional[ResultEnvelope] = None
        if record.status == ExecutionStatus.SUCCESS:
            result = self.results.load_envelope(call_id)

        return SubmitResult(
            call_id=call_id,
            status=record.status,
            outcome=self._outcome_for_status(record.status),
            result_id=record.result_id,
            result=result,
            error_type=record.error_type,
            error_message=record.error_message,
            attempt=record.attempt,
            resume_hint=self._hint_for_status(record.status),
            detail={
                "tool_name": record.tool_name,
                "worker_id": record.worker_id,
                "lease_id": record.lease_id,
                "started_at": record.started_at.isoformat() if record.started_at else None,
                "finished_at": record.finished_at.isoformat() if record.finished_at else None,
            },
        )

    def lookup(self, idempotency_key: str) -> Optional[SubmitResult]:
        """按幂等键查（§19 恢复流程的第一步）。

        返回 ``None`` 表示 **NOT_FOUND** —— 说明书特别强调这个分支：
        「说明可能之前根本没有成功提交 Tool，这时可以重新提交」。
        """
        record = self.idempotency.check(idempotency_key)
        if record is None:
            return None
        if record.call_id:
            existing = self.db.get_execution(record.call_id)
            if existing is not None:
                return self.query(record.call_id)
        return SubmitResult(
            call_id=record.call_id,
            status=(
                ExecutionStatus.SUCCESS
                if record.status == IdempotencyStatus.SUCCESS
                else ExecutionStatus.PROCESSING
                if record.status == IdempotencyStatus.PROCESSING
                else ExecutionStatus.FAILED
            ),
            outcome=self._outcome_for_status(
                ExecutionStatus.SUCCESS
                if record.status == IdempotencyStatus.SUCCESS
                else ExecutionStatus.PROCESSING
                if record.status == IdempotencyStatus.PROCESSING
                else ExecutionStatus.FAILED
            ),
            result_id=record.result_id,
            error_type=record.error_type,
            error_message=record.error,
            detail={"source": "idempotency", "lease_alive": self.leases.is_alive(record.call_id)},
        )

    def status(self, call_id: str) -> Optional[ExecutionStatus]:
        record = self.db.get_execution(call_id)
        return record.status if record else None

    def cancel(self, call_id: str) -> bool:
        """取消执行（§41 + §53 的 cancel 语义）。幂等。"""
        record = self.db.get_execution(call_id)
        if record is None:
            return False
        if record.status in (ExecutionStatus.SUCCESS, ExecutionStatus.CANCELLED):
            return True  # 已经结束，取消是 no-op
        self.db.update_execution(call_id, status=ExecutionStatus.CANCELLING)
        self.scheduler.cancel(call_id)
        self.db.update_execution(call_id, status=ExecutionStatus.CANCELLED)
        self.db.append_event(call_id, "tool.cancelled", {})
        return True

    def await_result(
        self,
        call_id: str,
        *,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.05,
    ) -> SubmitResult:
        """轮询等待结果，直到终态或超时（§4.2 的 Poll / Event 里的 Poll）。"""
        deadline = self.clock.time() + timeout_seconds
        last = self.query(call_id)
        while self.clock.time() < deadline:
            if last.status in (
                ExecutionStatus.SUCCESS,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
                ExecutionStatus.WAITING_HUMAN,
                ExecutionStatus.RECOVERY_REQUIRED,
            ):
                return last
            self.clock.sleep(poll_interval)
            # 真实环境里这是别的进程写的，必须重新读
            last = self.query(call_id)
        return last

    # ==================================================================
    # Outbox 投递（§46）
    # ==================================================================
    def publish_outbox(self, *, limit: int = 100) -> int:
        """把 DB 里已提交但尚未投递的 Outbox 事件推到 Redis，做到最终一致。

        §46 的顺序是「PostgreSQL Transaction 提交成功之后 -> Redis Update」。
        中间如果进程挂了，重启后这个方法会把积压的事件补发出去 ——
        这就是 Outbox 模式存在的意义：**不依赖分布式事务，也能避免状态撕裂**。
        """
        pending = self.db.fetch_unpublished_outbox(limit=limit)
        published = 0
        for event in pending:
            try:
                if event.event_type == "tool.completed":
                    key = event.payload.get("idempotency_key")
                    call_id = event.payload.get("call_id", "")
                    if key:
                        self.idempotency.complete_success(
                            key, call_id=call_id, result_id=event.payload.get("result_id")
                        )
                elif event.event_type == "tool.failed":
                    key = event.payload.get("idempotency_key")
                    if key:
                        self.idempotency.store.mark_failed(
                            key, call_id=event.payload.get("call_id", ""),
                            error=event.payload.get("error_message", ""),
                            error_type=event.payload.get("error_type", "internal_error"),
                        )
                self.db.mark_outbox_published(event.id)
                published += 1
            except Exception:  # noqa: BLE001 - 单条投递失败不影响其它
                logger.exception("outbox publish failed for %s", event.id)
        return published

    # ==================================================================
    # Agent 恢复入口（§19 / §55）
    # ==================================================================
    def resume_pending(self, pending: PendingToolCall | dict) -> SubmitResult:
        """Agent 崩溃重启后的恢复查询（§19 / §55 的分支图）。

        ====================== ==================================================
        幂等状态                动作
        ====================== ==================================================
        SUCCESS                直接取结果（不重跑）
        PROCESSING + 租约活着    WAIT
        PROCESSING + 租约过期    按幂等等级/风险决定 RETRY 或 HUMAN
        FAILED                  Recovery Policy
        NOT_FOUND               重新提交
        ====================== ==================================================
        """
        if isinstance(pending, dict):
            pending = PendingToolCall.model_validate(pending)

        result = self.lookup(pending.idempotency_key)
        if result is None:
            # NOT_FOUND：之前根本没成功提交 Tool，可以重新提交（§19）
            call = ToolCall(
                call_id=pending.call_id,
                tool_name=pending.tool_name,
                arguments=dict(pending.arguments),
                idempotency_key=pending.idempotency_key,
                attempt=pending.attempt + 1,
            )
            self.db.append_event(
                pending.call_id, "agent.resume.resubmit",
                {"reason": "幂等键 NOT_FOUND，判定为从未成功提交，重新提交"},
            )
            return self.submit(call)

        if result.status == ExecutionStatus.PROCESSING and self.leases.is_alive(result.call_id):
            result.resume_hint = RecoveryAction.WAIT
            result.detail["reason"] = "Tool 仍在执行且租约有效，等待完成"
        return result

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _check_loops(self, call: ToolCall) -> DuplicateCheck:
        """§30 重复检测 + §31 循环检测，合并成一个信号。"""
        check = DuplicateCheck()

        if self.duplicates is not None:
            verdict = self.duplicates.record(
                run_id=call.graph_run_id,
                step_id=call.logical_step_id,
                tool_name=call.tool_name,
                arguments=call.arguments,
            )
            if verdict.signal != LoopSignal.OK:
                check.reasons.append(f"重复执行检测: {verdict.reason}")
                check.degradation.update(verdict.degradation)
                self._incr("tool_duplicate_total", tool=call.tool_name)
                if self.audit:
                    self.audit.record(
                        call.call_id, "loop.duplicate",
                        signal=verdict.signal.value, count=verdict.count,
                        signature=verdict.signature,
                    )
            check.signal = self._max_signal(check.signal, verdict.signal)

        if self.cycles is not None:
            cycle = self.cycles.observe(run_id=call.graph_run_id, node=call.tool_name)
            if cycle.signal != LoopSignal.OK:
                check.reasons.append(f"DAG 循环检测: {cycle.reason}")
                if not check.degradation:
                    check.degradation.update(cycle.degradation)
                self._incr("tool_cycle_total", tool=call.tool_name)
                if self.audit:
                    self.audit.record(
                        call.call_id, "loop.cycle",
                        signal=cycle.signal.value, cycle_length=cycle.cycle_length,
                        repeated_nodes=cycle.repeated_nodes,
                    )
            check.signal = self._max_signal(check.signal, cycle.signal)

        return check

    @staticmethod
    def _max_signal(a: LoopSignal, b: LoopSignal) -> LoopSignal:
        order = [LoopSignal.OK, LoopSignal.WARNING, LoopSignal.DEGRADED, LoopSignal.STOP]
        return order[max(order.index(a), order.index(b))]

    def _reject(
        self, call: ToolCall, error_type: ErrorType, message: str, *, detail: dict | None = None
    ) -> SubmitResult:
        """被拦下的调用（不可重试）。

        注意这里**不写幂等记录** —— 参数错误、权限不足、注入命中都不代表
        「这个逻辑操作失败过」，下一次修正后提交应当被视为全新的一次尝试。
        """
        self.stats.rejected += 1
        self.db.upsert_execution(
            ExecutionRecord(
                call_id=call.call_id,
                run_id=call.graph_run_id,
                agent_id=call.agent_id,
                tenant_id=call.tenant_id,
                tool_name=call.tool_name,
                tool_version=call.tool_version,
                idempotency_key=call.idempotency_key,
                arguments=dict(call.arguments),
                arguments_hash=arguments_hash(call.arguments),
                status=ExecutionStatus.FAILED,
                error_type=error_type.value,
                error_message=message,
                finished_at=utcnow(),
            )
        )
        if self.audit:
            self.audit.record(
                call.call_id, "gateway.rejected",
                error_type=error_type.value, message=message,
            )
        self._incr("tool_failure_total", tool=call.tool_name)
        return SubmitResult(
            call_id=call.call_id,
            status=ExecutionStatus.FAILED,
            outcome=SubmitOutcome.REJECTED.value,
            error_type=error_type.value,
            error_message=message,
            resume_hint=(
                RecoveryAction.REPAIR
                if error_type == ErrorType.VALIDATION_ERROR
                else RecoveryAction.ABORT
            ),
            detail=detail or {},
        )

    @staticmethod
    def _outcome_for_status(status: ExecutionStatus) -> str:
        if status == ExecutionStatus.SUCCESS:
            return SubmitOutcome.EXECUTED.value
        if status == ExecutionStatus.CANCELLED:
            return SubmitOutcome.REJECTED.value
        if status == ExecutionStatus.WAITING_HUMAN:
            return SubmitOutcome.WAITING_HUMAN.value
        if status.is_pending:
            return SubmitOutcome.WAITING.value
        return SubmitOutcome.FAILED.value

    @staticmethod
    def _hint_for_status(status: ExecutionStatus) -> Optional[RecoveryAction]:
        if status.is_pending:
            return RecoveryAction.WAIT
        if status == ExecutionStatus.WAITING_HUMAN:
            return RecoveryAction.HUMAN
        if status in (ExecutionStatus.FAILED, ExecutionStatus.RECOVERY_REQUIRED):
            return RecoveryAction.RETRY
        return None

    def _is_approved(self, call_id: str) -> bool:
        """这张高风险调用是否已经拿到人工放行（§52）。

        只有 ``APPROVED`` / ``MODIFIED`` 算放行。``WAITING_HUMAN`` 不算，
        ``REJECTED`` 更不算 —— 被驳回的单子不该因为「再提交一次」而复活。
        """
        if self.approvals is None:
            return False
        record = self.approvals.get(call_id)
        if record is None:
            return False
        return getattr(record, "status", "") in ("APPROVED", "MODIFIED")

    def _incr(self, name: str, **labels: str) -> None:
        if self.metrics is not None:
            self.metrics.incr(name, **labels)

    # ------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "tools": self.registry.names(),
            "stats": self.stats.__dict__,
            "queue": self.scheduler.queue_stats(),
        }
