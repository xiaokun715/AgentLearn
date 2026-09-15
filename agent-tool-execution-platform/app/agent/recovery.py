"""Agent 崩溃恢复 —— §19 / §20 / §55 / §56 / §57。

先看 §19 给出的那张分支图（:meth:`AgentRecoveryManager.plan_resume` 就是它的实现）::

    Load Checkpoint
      |
      +-- pending_tool_call 存在？
            |
            +-- Query Idempotency（按 idempotency_key 查，不是按 call_id 查）
                  |
                  +-- SUCCESS      -> 直接取结果（Load Result），**绝不重跑**
                  |
                  +-- PROCESSING   -> Check Lease
                  |                    |
                  |                    +-- Alive   -> WAIT（别人在跑，等就行）
                  |                    |
                  |                    +-- Expired -> Recover
                  |                          （按 §56/§57：RETRY 还是 HUMAN）
                  |
                  +-- FAILED       -> Recovery Policy（§35 错误分类 -> 动作）
                  |
                  +-- NOT_FOUND    -> Resubmit
                        （幂等键不存在 = 这次调用很可能**根本没成功提交出去**，
                          重提交是安全的；也可能只是幂等键过期了，
                          见 AgentRecoveryManager.plan_resume 里的讨论）

§20：为什么必须是「Checkpoint + Idempotency」
----------------------------------------------
两者解决的是**两个不同问题**，缺一个都会出事：

============================ ==================================== ====================================
场景                          只有 Checkpoint                      只有 Idempotency
============================ ==================================== ====================================
Agent 崩溃在第 3 步            CP 知道「第 3 步提交过了」             不知道第 3 步是什么
Tool 有没有真跑               不知道（CP 不记副作用）                知道（幂等键 SUCCESS）
恢复后该干什么                重跑第 3 步 -> **可能重复副作用**        从头再来 -> 结果对但成本翻倍
能不能查到结果                不能（没有 call_id）                  能（幂等键 -> call_id -> 结果）
============================ ==================================== ====================================

反过来，如果**两个都有**，恢复就变成一次确定性查表：读 Checkpoint 拿到
``pending_tool_call``（进度 + call_id + 幂等键），用幂等键问平台
「这笔做过没有」，然后按四种状态各走各的路 —— 既不会重复扣款，
也不会丢掉已经跑完的那一步。

§55/§56/§57 的三个补充判据
---------------------------
* **§56 租约**：``PROCESSING`` 不等于「正在跑」。Worker A 可能已经死了、
  或者只是断网。判活的唯一依据是**租约**：租约还活着说明有 Worker 在续租，
  等就行；租约过期才谈得上接管。
* **§57 幂等性等级**：接管（重跑）是否安全，取决于 Tool 的幂等性等级。
  ``PURE`` / ``IDEMPOTENT`` 可以重跑；``AT_LEAST_ONCE`` / ``NON_IDEMPOTENT``
  重跑会产生第二次副作用 —— 那就只能转人工。
* **§51 风险等级**：即使是幂等 Tool，高风险（``database_delete`` 之类）
  也不允许平台自作主张接管，必须人工确认（``config.recovery``
  里的 ``uncertain_high_risk_action`` / ``uncertain_low_risk_action`` 就是这两条）。

一句话总结：**「能不能自动接管」= 租约过期 ∧ 幂等性等级允许 ∧ 风险等级允许。**
三个条件缺一不可，任何一个不满足就转人工 —— 这是「宁可多问一句，
不要多扣一笔」的工程表达。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from ..config import AppConfig
from ..domain.enums import (
    ErrorType,
    ExecutionStatus,
    IdempotencyLevel,
    IdempotencyStatus,
    RecoveryAction,
    RiskLevel,
    SubmitOutcome,
)
from ..domain.errors import ToolPlatformError
from ..domain.models import (
    ExecutionRecord,
    PendingToolCall,
    ResultEnvelope,
    SubmitResult,
    ToolCall,
    utcnow,
)

logger = logging.getLogger(__name__)

#: §12 幂等命中 SUCCESS 之后，SubmitResult 里的 outcome 取值。
_SUCCESS_OUTCOMES = (SubmitOutcome.EXECUTED.value, SubmitOutcome.DEDUPLICATED.value)


@dataclass
class ResumePlan:
    """恢复决策 —— 「这一笔调用现在该怎么办」的完整结论。

    :param action: §35 的动作。取值与含义的对应关系：

        * ``RETRY``  —— 重新提交（幂等键不存在，或租约过期且允许接管）
        * ``WAIT``   —— 什么都不做，等（别人在跑，租约有效）
        * ``HUMAN``  —— 转人工（幂等性等级不允许重跑，或高风险）
        * ``ABORT``  —— **不推进**。注意它在这里的语义是「平台不再自己往前走」，
          包括「结果已经拿到了，直接用，不必再执行」这一种
          （见 :attr:`result`）；排障时请以 :attr:`reason` 为准，
          不要只看 ``action`` 就脑补成「放弃」。
    :param status: 平台的 :class:`~app.domain.enums.ExecutionStatus`；查不到时为 ``None``。
    :param call_id: 这次调用的 ID（回查结果、判租约都要它）。
    :param idempotency_key: §7 的幂等键 —— **恢复流程的钥匙**。
        没有它就只能按 ``call_id`` 查，而 ``call_id`` 是每次提交新生成的，
        跨进程重启后除了 Checkpoint 没人记得。
    :param result: 已经拿到的结果（``SUCCESS`` 分支唯一的产出）。
        非空即代表「这一步已经完成，直接消费，绝不重跑」。
    :param reason: 一句可直接念出来的中文，说明「为什么是这个动作」。
        恢复决策事后最需要的就是这句话 —— 光看 ``action`` 看不出判据。
    :param should_resubmit: 是否需要重新 ``submit``。与 ``action == RETRY`` 基本同义，
        单独留一个 bool 是为了让调用方不必枚举 ``RecoveryAction`` 就能分支
        （演示脚本里 ``if plan.should_resubmit:`` 比 ``if plan.action is RETRY:`` 好读）。
    :param should_wait: 是否需要等待（``gateway.await_result``）。
    :param escalate_to_human: 是否需要拉起人工审批单（§51）。

    ---- 以下是 §19 字段清单之外的**补充** ----
    :param tool_name: 重提交时重建 :class:`~app.domain.models.ToolCall` 需要它。
        没有这个字段，:meth:`AgentRecoveryManager.apply` 就无从重建调用，
        ``RESUBMIT`` 这条分支会变成一句空话。
    :param arguments: 同上，重提交所需的参数原样。
    :param run_id: 重建调用时填 ``ToolCall.graph_run_id`` 用。
        它不影响防重结果（幂等键是显式钉死的，见 ``apply``），
        但会让审计链路里的 ``run_id`` 对上，排障时不必再去猜。
    :param tenant_id: 同上，重建调用时填 ``ToolCall.tenant_id``。
    """

    action: RecoveryAction
    status: Optional[ExecutionStatus]
    call_id: Optional[str]
    idempotency_key: Optional[str]
    result: Optional[ResultEnvelope]
    reason: str
    should_resubmit: bool = False
    should_wait: bool = False
    escalate_to_human: bool = False

    # ---- 补充：让 apply() 能重建 ToolCall ----
    tool_name: Optional[str] = None
    arguments: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    tenant_id: str = "tenantA"

    # ---- 补充：上下游判据，供审计与演示打印 ----
    risk_level: RiskLevel = RiskLevel.LOW
    idempotency_level: IdempotencyLevel = IdempotencyLevel.AT_LEAST_ONCE
    lease_alive: Optional[bool] = None
    attempt: int = 0

    @property
    def should_consume_result(self) -> bool:
        """是否应该**直接消费** :attr:`result` 并继续往下走（而不是重跑或等待）。

        这是对 ``action == ABORT`` 这个反直觉取值的显式化。§12 的
        「幂等命中 SUCCESS -> 直接返回缓存结果，绝对不能再次执行」在动作枚举里
        没有自己的名字，本实现把它归到 ``ABORT``（= 平台不再自己往前走）。
        但排障时看到 ``ABORT`` 很容易脑补成「放弃」，于是这里给一个自解释的出口：

        ::

            if plan.should_consume_result:   # 已经做完了，拿结果继续
                ...
            elif plan.should_resubmit:       # 从没提交成功，重提交
                ...
            elif plan.should_wait:           # 别人在跑，等
                ...
            else:                            # 转人工
                ...
        """
        return self.result is not None

    def to_dict(self) -> dict:
        """JSON-safe 视图，用于打日志 / 追加 ``agent.resumed`` 事件。"""
        return {
            "action": self.action.value,
            "status": self.status.value if self.status else None,
            "call_id": self.call_id,
            "idempotency_key": self.idempotency_key,
            "has_result": self.result is not None,
            "reason": self.reason,
            "should_resubmit": self.should_resubmit,
            "should_wait": self.should_wait,
            "escalate_to_human": self.escalate_to_human,
            "tool_name": self.tool_name,
            "risk_level": self.risk_level.value,
            "idempotency_level": self.idempotency_level.value,
            "lease_alive": self.lease_alive,
            "attempt": self.attempt,
        }


class AgentRecoveryManager:
    """读 Checkpoint -> 查幂等 -> 判租约 -> 给出恢复动作（§19 / §55）。

    :param gateway: :class:`ToolGateway`。恢复流程只用到它的**读**能力
        （``lookup`` / ``query`` / ``status`` / ``await_result`` / ``submit``），
        不用 ``cancel`` —— 恢复意味着「把这件事做完」，而取消它属于另一个决策。
    :param db: :class:`~app.infra.database.Database`。用途有三个：
        写 ``agent.resumed`` 审计事件（§42）、按 ``run_id`` 找出未完成的执行记录、
        以及在网关没带出风险/幂等性等级时从执行记录里补。
    :param idempotency: :class:`~app.idempotency.manager.IdempotencyManager`，
        网关的 ``lookup`` 不可用时的**兜底**查询通道。
        **为什么要有兜底**：`ToolGateway` 是平台的门面，不同实现（内存版 / HTTP 版）
        暴露的读能力不一定一样；恢复这条链路不允许因为「门面少了半个方法」而失效。
    :param lease_manager: :class:`~app.lease.manager.LeaseManager`，``is_alive(call_id)``
        是 §56 唯一的判活依据。
    :param config: 平台配置。``config.recovery`` 提供
        「错误 -> 动作」表与「租约过期时按风险等级分流」的两条策略。
    """

    def __init__(
        self,
        gateway: Any,
        db: Any,
        idempotency: Any,
        lease_manager: Any,
        config: Optional[AppConfig] = None,
    ) -> None:
        self.gateway = gateway
        self.db = db
        self.idempotency = idempotency
        self.lease_manager = lease_manager
        self.config = config or AppConfig.for_demo()

    # ==================================================================
    # 主入口：§19 分支图
    # ==================================================================
    def plan_resume(self, pending: dict | PendingToolCall) -> ResumePlan:
        """§19 / §55 那张分支图的逐分支实现。

        **判据来源的优先级**（越靠前越权威）：

        1. ``gateway.lookup(idempotency_key)`` —— 协调层，实时且权威（§8）。
        2. ``gateway.query(call_id)`` —— 按 call_id 查，用于兜「幂等键缺失/过期」。
        3. ``idempotency.check(key)`` —— 直连幂等管理器（网关没暴露 lookup 时）。
        4. DB 里的 ``tool_execution`` 记录 —— 最终事实（§44），但可能滞后于
           Redis，所以只作为**最后**的补全手段。

        :param pending: Checkpoint 里的 ``pending_tool_call``（dict 或
            :class:`~app.domain.models.PendingToolCall`）。
        """
        pending = self._normalize_pending(pending)
        key = pending.idempotency_key
        call_id = pending.call_id
        tool_name = pending.tool_name

        # 空 pending 必须**硬失败**，不能往下走。
        #
        # 为什么这是安全闸门而不是参数校验：下面「查不到」那一支会得出
        # 「很可能根本没成功提交，重提交是安全的」，然后 should_resubmit=True。
        # 如果 pending 是空的（调用方传了 None、或者从 Checkpoint 里挖错了层级，
        # 比如把 {"status":..., "values":{...}} 的顶层当成了 state），
        # 那么「查不到」是必然的 —— 但结论「安全重提交」就是**凭空捏造的**。
        # 对 NON_IDEMPOTENT 的 Tool（payment / database.delete），
        # 这一下就是第二次真实副作用。宁可在这里炸掉，也不要静默重跑。
        if not call_id and not key:
            raise ValueError(
                "pending_tool_call 为空（call_id 与 idempotency_key 都没有）："
                "无法判定该等待、该取结果还是该重提交。"
                "请确认传入的是 Checkpoint 快照里的 values.pending_tool_call，"
                "而不是快照顶层 —— 静默按「未提交」处理会导致重复副作用。"
            )

        logger.info(
            "恢复判定开始: call=%s key=%s tool=%s", call_id, key, tool_name
        )

        # 判据：先取平台侧关于这笔调用的最新说法
        submit = self._lookup_by_key(key) or self._query_by_call(call_id)
        record = self._execution_record(call_id, key)

        risk = self._resolve_risk(tool_name, submit, record, pending)
        level = self._resolve_idempotency_level(tool_name, submit, record)

        base = {
            "call_id": call_id,
            "idempotency_key": key,
            "tool_name": tool_name,
            "arguments": dict(pending.arguments or {}),
            # run_id / tenant_id 优先取 DB 事实记录：Checkpoint 里的 pending
            # 不含这两个字段（§18 的 PendingToolCall 只留恢复必需的最小集）。
            "run_id": str(getattr(record, "run_id", "") or ""),
            "tenant_id": str(getattr(record, "tenant_id", "") or "tenantA"),
            "attempt": pending.attempt,
            "risk_level": risk,
            "idempotency_level": level,
        }

        # ------------------------------------------------------------ 查不到
        if submit is None:
            return ResumePlan(
                action=RecoveryAction.RETRY,
                status=None,
                result=None,
                should_resubmit=True,
                reason=(
                    "幂等键与 call_id 都查不到记录 —— 说明这次调用很可能"
                    "**根本没有成功提交出去**（提交前崩溃），重提交是安全的。"
                    "注意：若幂等键的 TTL 短于崩溃恢复耗时，也会落到这里，"
                    "这种情况必须靠幂等 TTL > 恢复耗时来规避（§8 里那条 TTL 约束）"
                ),
                **base,
            )

        idem_status = self._idempotency_status_of(submit, key)

        # --------------------------------------------------- SUCCESS：直接取结果
        if idem_status == IdempotencyStatus.SUCCESS or submit.is_final:
            result = self._load_result(submit, call_id)
            if result is not None:
                reason = (
                    "幂等命中 SUCCESS（§12）：这一步**已经真的做完了**，"
                    "直接把结果装回图里继续下一步。绝不重跑 —— "
                    "重跑一笔已成功的扣款就是事故"
                )
            else:
                # 「状态说成功，但结果取不到」是一件必须被看见的事，
                # 不能静默降级成「没结果」。常见原因是结果落在
                # Object Storage 而 artifact 引用丢了（§39），
                # 或者是 Result Processor 与状态表之间出现了撕裂（§45）。
                reason = (
                    "幂等命中 SUCCESS 但**拿不到 ResultEnvelope**："
                    "状态说这笔已经成功，结果却取不回来。需要人工核对"
                    "（结果落 artifact 时引用可能已失效，见 §39/§45）"
                )
            return ResumePlan(
                action=RecoveryAction.ABORT,  # 不再推进执行；结果已在手（或需人工）
                status=submit.status,
                result=result,
                should_resubmit=False,
                should_wait=False,
                escalate_to_human=result is None,
                reason=reason,
                **base,
            )

        # ---------------------------------------------- PROCESSING：查租约（§56）
        if idem_status == IdempotencyStatus.PROCESSING or submit.status.is_pending:
            alive = self._lease_alive(call_id)

            if alive:
                return ResumePlan(
                    action=RecoveryAction.WAIT,
                    status=submit.status,
                    result=None,
                    should_wait=True,
                    lease_alive=True,
                    reason=(
                        f"幂等键命中 PROCESSING 且租约**仍有效** —— 有 Worker 在续租，"
                        f"说明它没死、还在跑。此时重复提交会被幂等层挡掉，"
                        f"接管更会造成双跑，唯一正确的动作是等（§11/§56）"
                    ),
                    **base,
                )

            # 租约过期 -> 要不要接管（§56/§57/§51 三个条件）
            can_takeover = level.crash_safe_to_retry and risk != RiskLevel.HIGH
            if can_takeover:
                return ResumePlan(
                    action=RecoveryAction.RETRY,
                    status=submit.status,
                    result=None,
                    should_resubmit=True,
                    lease_alive=False,
                    reason=(
                        f"租约已过期且幂等性等级为 {level.value}（§57 允许接管重跑）、"
                        f"风险等级 {risk.value}（§51 未触发人工）-> 由本 Worker 接管重跑"
                    ),
                    **base,
                )

            return ResumePlan(
                action=RecoveryAction.HUMAN,
                status=ExecutionStatus.RECOVERY_REQUIRED,
                result=None,
                escalate_to_human=True,
                lease_alive=False,
                reason=(
                    f"租约已过期，但**不允许**自动接管：幂等性等级 "
                    f"{level.value}（重跑会产生第二次副作用，§57）"
                    f"或风险等级 {risk.value}（§51 高风险必须人工）。"
                    f"转人工确认这笔调用到底有没有生效（§56）"
                ),
                **base,
            )

        # ------------------------------------------------ FAILED：走恢复策略（§35）
        error_type = self._error_type_of(submit, record)
        action = self._recovery_action_for(error_type, tool_name, risk, pending.attempt)

        if action == RecoveryAction.RETRY:
            return ResumePlan(
                action=action,
                status=ExecutionStatus.FAILED,
                result=None,
                should_resubmit=True,
                reason=(
                    f"上次执行失败（{error_type.value}），按 §35 恢复策略表判为 RETRY："
                    f"错误类型在 Tool 的 retry_on 白名单内且次数未用尽"
                ),
                **base,
            )

        if action == RecoveryAction.HUMAN:
            return ResumePlan(
                action=action,
                status=ExecutionStatus.WAITING_HUMAN,
                result=None,
                escalate_to_human=True,
                reason=f"上次执行失败（{error_type.value}）且策略判为 HUMAN（§35/§51）",
                **base,
            )

        return ResumePlan(
            action=action,
            status=ExecutionStatus.FAILED,
            result=None,
            should_resubmit=False,
            reason=(
                f"上次执行失败（{error_type.value}），§35 判为 {action.value}："
                f"该错误不可重试或已用尽次数。**不自动重跑**是刻意的 —— "
                f"让永久性故障变成无限重放比直接失败更糟（§75）"
            ),
            **base,
        )

    # ==================================================================
    # 落地动作
    # ==================================================================
    def apply(self, plan: ResumePlan) -> dict:
        """把 :class:`ResumePlan` 变成一个真实动作，并留下 ``agent.resumed`` 审计事件。

        四条分支：

        * ``should_wait``  -> ``gateway.await_result(call_id)`` 阻塞到出结果（有限超时）
        * ``should_resubmit`` -> 按原参数、**原幂等键**重新 ``submit``
        * ``escalate_to_human`` -> 只返回「需要人工」的信号，不代替人去决定
        * 其余（``SUCCESS`` 那条）-> 什么都不做，结果已经在 :attr:`ResumePlan.result` 里

        注意重提交时幂等键**原样带过去**：§7 的键由
        ``tenant + run + step + tool + args`` 决定，恢复时这四个分量一个都没变，
        所以算出来的键必须和首次提交一模一样 —— 一旦变了，
        平台的幂等层就认不出这是同一笔，防重直接失效。
        """
        result: dict[str, Any] = {
            "action": plan.action.value,
            "call_id": plan.call_id,
            "tool_name": plan.tool_name,
            "reason": plan.reason,
            "plan": plan.to_dict(),
        }

        if plan.result is not None:
            result["status"] = "RESULT_LOADED"
            result["result"] = plan.result.to_agent_view()
            self._audit(plan, outcome="result_loaded")
            return result

        if plan.should_wait:
            try:
                submit = self.gateway.await_result(
                    plan.call_id, timeout_seconds=float(self.config.lease_ttl_seconds)
                )
                result["status"] = "WAITED"
                result["submit_result"] = _submit_view(submit)
                self._audit(plan, outcome="waited", extra={"status": submit.status.value})
            except Exception as exc:
                # 等待超时不是错误：Tool 可能就是要跑很久。
                # 把「还在等」如实报出去，由调用方决定再等一轮还是转人工。
                result["status"] = "WAIT_TIMEOUT"
                result["error"] = str(exc)
                self._audit(plan, outcome="wait_timeout", extra={"error": str(exc)})
            return result

        if plan.should_resubmit:
            call = self._rebuild_call(plan)
            try:
                submit = self.gateway.submit(call)
                result["status"] = "RESUBMITTED"
                result["new_call_id"] = call.call_id
                result["submit_result"] = _submit_view(submit)
                self._audit(
                    plan,
                    outcome="resubmitted",
                    extra={"new_call_id": call.call_id, "outcome": str(submit.outcome)},
                )
            except ToolPlatformError as exc:
                result["status"] = "RESUBMIT_REJECTED"
                result["error"] = exc.to_dict()
                self._audit(plan, outcome="resubmit_rejected", extra=exc.to_dict())
            except Exception as exc:  # pragma: no cover - 网关自身故障
                result["status"] = "RESUBMIT_FAILED"
                result["error"] = str(exc)
                self._audit(plan, outcome="resubmit_failed", extra={"error": str(exc)})
            return result

        if plan.escalate_to_human:
            result["status"] = "NEEDS_HUMAN"
            result["escalate_to_human"] = True
            result["approval_request"] = {
                "call_id": plan.call_id,
                "tool_name": plan.tool_name,
                "arguments": plan.arguments,
                "risk_level": plan.risk_level.value,
                "idempotency_level": plan.idempotency_level.value,
                "reason": plan.reason,
                "options": ["APPROVE", "REJECT", "MODIFY"],
            }
            self._audit(plan, outcome="escalated_to_human")
            return result

        result["status"] = "NO_ACTION"
        self._audit(plan, outcome="no_action")
        return result

    def recover_run(
        self, *, run_id: str, pending: dict | PendingToolCall | None = None
    ) -> ResumePlan:
        """一次完整的恢复：找到「上次卡在哪」-> 给出动作。

        :param pending: 优先由调用方从 Checkpoint 里取（``AgentRuntime.state(run_id)``
            的 ``pending_tool_call``）—— 那是**最权威**的来源，因为它和
            ``step_index`` 一起被原子地写下来，两者必然一致。
        :param run_id: 没给 ``pending`` 时的兜底：从 DB 的 ``tool_execution`` 里
            找这个 run 下**还没进终态**的那一条。这条路径的语义弱一些
            （DB 可能滞后于 Redis，也可能同时有多条未完成记录），
            仅用于「Checkpoint 丢了但 DB 还在」的灾难场景。
        """
        if pending is None:
            pending = self._pending_from_db(run_id)
            if pending is None:
                return ResumePlan(
                    action=RecoveryAction.ABORT,
                    status=None,
                    call_id=None,
                    idempotency_key=None,
                    result=None,
                    reason=(
                        f"run={run_id} 没有任何未完成的执行记录，也没有传入 pending —— "
                        f"要么这个 run 从来没提交过 Tool，要么它已经全部完成了。"
                        f"本方法不负责「判断整个 run 是否完成」，那是 Checkpoint 的职责"
                    ),
                )
            logger.info("run=%s 从 DB 兜底恢复 pending=%s", run_id, pending.call_id)

        return self.plan_resume(pending)

    # ==================================================================
    # 判据采集
    # ==================================================================
    def _lookup_by_key(self, key: Optional[str]) -> Optional[SubmitResult]:
        """按幂等键查 —— 恢复流程的**首选**查询通道（§8/§12）。

        为什么优先按幂等键而不是按 ``call_id``：
        ``call_id`` 是每次提交新生成的，而幂等键是**可以由任何人重算出来**的
        （§7 的四个分量都在手边）。恢复方未必拿得到原 call_id（比如 Checkpoint
        写了一半就崩了），但只要参数还在，幂等键就能算出来，就能查到这笔。
        """
        if not key:
            return None
        lookup = getattr(self.gateway, "lookup", None)
        if callable(lookup):
            try:
                found = lookup(key)
                if found is not None:
                    return found
            except Exception as exc:
                logger.warning("gateway.lookup(%s) 失败: %s", key, exc)

        # 兜底：直连幂等管理器。返回的 IdempotencyRecord 只有状态与 result_id，
        # 没有 ResultEnvelope —— 所以这里只把它「翻译」成一个最小可判的 SubmitResult。
        check = getattr(self.idempotency, "check", None)
        if callable(check):
            try:
                record = check(key)
            except Exception as exc:
                logger.warning("idempotency.check(%s) 失败: %s", key, exc)
                return None
            if record is None:
                return None
            status = {
                IdempotencyStatus.SUCCESS: ExecutionStatus.SUCCESS,
                IdempotencyStatus.PROCESSING: ExecutionStatus.PROCESSING,
                IdempotencyStatus.FAILED: ExecutionStatus.FAILED,
            }.get(record.status, ExecutionStatus.PROCESSING)
            return SubmitResult(
                call_id=record.call_id or "",
                status=status,
                outcome=SubmitOutcome.WAITING.value,
                result_id=record.result_id,
                error_type=record.error_type,
                error_message=record.error,
                detail={"source": "idempotency_manager"},
            )
        return None

    def _query_by_call(self, call_id: Optional[str]) -> Optional[SubmitResult]:
        """按 ``call_id`` 查当前状态。

        什么时候会走到这里：幂等键已经过期（§8 的 TTL 到了），
        但 ``call_id`` 还躺在 Checkpoint 或 DB 里。
        """
        if not call_id:
            return None
        query = getattr(self.gateway, "query", None)
        if not callable(query):
            return None
        try:
            return query(call_id)
        except Exception as exc:
            logger.warning("gateway.query(%s) 失败: %s", call_id, exc)
            return None

    def _execution_record(
        self, call_id: Optional[str], key: Optional[str]
    ) -> Optional[ExecutionRecord]:
        """从 DB 取执行记录 —— 风险等级/幂等性等级的**最后**一个来源。"""
        if self.db is None:
            return None
        try:
            if call_id:
                record = self.db.get_execution(call_id)
                if record is not None:
                    return record
            if key:
                return self.db.get_execution_by_idempotency_key(key)
        except Exception as exc:  # pragma: no cover
            logger.warning("读取 execution 记录失败: %s", exc)
        return None

    def _lease_alive(self, call_id: Optional[str]) -> bool:
        """§56 判活。**查不到租约 = 已过期**，不是「不知道」。

        这个默认值是刻意的、也是安全的：租约是**必须主动续**的
        （§15 每 10s 一次心跳），没续上就说明持有者已经不在了。
        反过来「查不到就当活着」会让系统永远等下去 —— 一个死掉的 Worker
        留下的租约永远不会自己变成「已知过期」。
        """
        if not call_id or self.lease_manager is None:
            return False
        try:
            return bool(self.lease_manager.is_alive(call_id))
        except Exception as exc:
            logger.warning("租约判活失败 call=%s: %s", call_id, exc)
            return False

    # ==================================================================
    # 判据补全（风险 / 幂等性等级 / 错误分类）
    # ==================================================================
    def _resolve_risk(
        self,
        tool_name: str,
        submit: Optional[SubmitResult],
        record: Optional[ExecutionRecord],
        pending: PendingToolCall,
    ) -> RiskLevel:
        """风险等级的三个来源，按可靠性排序：平台返回值 > DB 记录 > 配置声明。

        为什么不能只看配置：YAML 里写的是**声明值**，
        而真正拦截这次调用的是平台的 Risk Engine —— 它可能因为运行时的
        额外上下文（比如目标表是生产表）把等级提上去了。
        恢复决策必须用**当时真实的**等级，否则会把一个 HIGH 误判成 LOW 去自动重跑。
        """
        detail = (submit.detail if submit is not None else {}) or {}
        raw = detail.get("risk_level") or getattr(record, "risk_level", None)
        if raw:
            try:
                return RiskLevel(str(raw).lower())
            except ValueError:
                pass
        override = self.config.tool_override(tool_name)
        if override is not None and override.risk:
            try:
                return RiskLevel(str(override.risk).lower())
            except ValueError:
                pass
        return RiskLevel.LOW

    def _resolve_idempotency_level(
        self,
        tool_name: str,
        submit: Optional[SubmitResult],
        record: Optional[ExecutionRecord],
    ) -> IdempotencyLevel:
        """幂等性等级（§57）—— 决定「租约过期后能不能直接接管重跑」。

        默认值取 ``AT_LEAST_ONCE`` 而不是 ``PURE``：**不知道就别乱重跑**。
        把默认设成 PURE 会让「没声明等级的 Tool」默认获得自动接管权，
        那等于把安全性建立在「大家都记得声明」这个假设上。
        """
        detail = (submit.detail if submit is not None else {}) or {}
        raw = detail.get("idempotency_level") or getattr(
            record, "idempotency_level", None
        )
        if raw:
            try:
                return IdempotencyLevel(str(raw))
            except ValueError:
                pass
        override = self.config.tool_override(tool_name)
        if override is not None and override.idempotency_level:
            try:
                return IdempotencyLevel(str(override.idempotency_level))
            except ValueError:
                pass
        return IdempotencyLevel.AT_LEAST_ONCE

    def _error_type_of(
        self, submit: Optional[SubmitResult], record: Optional[ExecutionRecord]
    ) -> ErrorType:
        """错误分类（§34）。取不到就归 ``INTERNAL_ERROR`` —— 未知错误不重试。"""
        raw = None
        if submit is not None:
            raw = submit.error_type
            if raw is None and submit.detail:
                raw = submit.detail.get("error_type")
        if raw is None and record is not None:
            raw = record.error_type
        if raw:
            try:
                return ErrorType(str(raw))
            except ValueError:
                pass
        return ErrorType.INTERNAL_ERROR

    def _recovery_action_for(
        self,
        error_type: ErrorType,
        tool_name: str,
        risk: RiskLevel,
        attempt: int,
    ) -> RecoveryAction:
        """§35 恢复策略表 + 三条闸门。

        策略表来自 ``config.recovery``（YAML 可覆盖），这里再补两道说明书里
        明说但表里没写的闸门：

        * **次数用尽** -> ``ABORT``（§75「不会无限 Retry」）。
          次数取 Tool 声明的 ``max_attempts``，用 ``attempt`` 对比。
        * **高风险** -> 即使表说 RETRY 也降级 ``HUMAN``（§51）。
          有不可撤销副作用的操作，不能由平台自己决定再来一遍。

        注意这里**没有**复用 :class:`~app.recovery.policy.RecoveryPolicyEngine`：
        那个引擎需要 ``ToolRegistry`` 才能拿到 ``retry_on`` / ``fallback_tool``，
        而本管理器按 §19 的接口只收 gateway/db/idempotency/lease 四件套。
        少的那部分信息用配置里的覆盖声明补足（``config.effective_retry``），
        这样既不扩大接口，也不至于把「次数上限」这个硬边界丢掉。
        """
        retry_policy = self.config.effective_retry(tool_name)
        action = self.config.recovery.action_for(error_type)

        if action == RecoveryAction.RETRY:
            if attempt >= retry_policy.max_attempts:
                return RecoveryAction.ABORT
            if not retry_policy.allows(error_type):
                return RecoveryAction.ABORT
        if action == RecoveryAction.FALLBACK:
            # 本管理器不知道 fallback 是谁（那在 Registry 里），
            # 无法自己换 Tool 执行。交给图上的循环降级或人工处理。
            return RecoveryAction.HUMAN
        if risk == RiskLevel.HIGH and action not in (
            RecoveryAction.ABORT,
            RecoveryAction.HUMAN,
        ):
            return RecoveryAction.HUMAN
        return action

    def _load_result(
        self, submit: SubmitResult, call_id: Optional[str]
    ) -> Optional[ResultEnvelope]:
        """取结果（§39/§40）。

        三条路，按成本从低到高：``SubmitResult.result`` 已经在手就用它；
        没有就再 ``query`` 一次（有些网关只在终态时才把 result 装进返回值）；
        再没有就只能返回 ``None`` —— 并在 reason 里说清楚
        「结果是 SUCCESS 但取不到」，因为那是一个必须被人看见的异常，
        而不是可以静默降级成「没结果」的小事。
        """
        if submit.result is not None:
            return submit.result

        if call_id:
            refresh = self._query_by_call(call_id)
            if refresh is not None and refresh.result is not None:
                return refresh.result

        logger.warning(
            "幂等状态为 SUCCESS 但拿不到 ResultEnvelope: call=%s", call_id or submit.call_id
        )
        return None

    # ==================================================================
    # 小工具
    # ==================================================================
    @staticmethod
    def _normalize_pending(pending: dict | PendingToolCall) -> PendingToolCall:
        """把 Checkpoint 里的 dict 还原成 :class:`PendingToolCall`。

        注意 ``submitted_at`` 可能是 ISO 字符串（JSON 落了盘就变字符串了），
        Pydantic 能自己解析；解析不了时用当前时间兜底 —— 这个字段只用于展示，
        不该因为它格式不对就中断恢复。
        """
        if isinstance(pending, PendingToolCall):
            return pending
        payload = dict(pending or {})
        raw_time = payload.get("submitted_at")
        if isinstance(raw_time, str):
            try:
                payload["submitted_at"] = datetime.fromisoformat(raw_time)
            except ValueError:
                payload["submitted_at"] = utcnow()
        try:
            return PendingToolCall.model_validate(payload)
        except Exception:
            # 最坏情况：只有 call_id / 幂等键也能继续（两个都是恢复必需的）
            return PendingToolCall(
                call_id=str(payload.get("call_id") or ""),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                tool_name=str(payload.get("tool_name") or ""),
                arguments=dict(payload.get("arguments") or {}),
            )

    def _pending_from_db(self, run_id: str) -> Optional[PendingToolCall]:
        """从 ``tool_execution`` 里找这个 run 未完成的调用。

        「未完成」的判据用 :attr:`ExecutionStatus.is_pending`（§41 状态机里
        所有非终态的中间状态），加上 ``FAILED``：失败也是需要恢复处置的状态。
        ``RECOVERY_REQUIRED`` 是最明确的信号 —— 状态机专门为「租约过期且
        执行状态不确定」（§56）标出来的那一格。
        """
        if self.db is None:
            return None
        try:
            records = self.db.list_executions(run_id)
        except Exception as exc:  # pragma: no cover
            logger.warning("按 run_id 列执行记录失败: %s", exc)
            return None

        candidates = [
            record
            for record in records
            if record.status.is_pending
            or record.status
            in (ExecutionStatus.FAILED, ExecutionStatus.RECOVERY_REQUIRED)
        ]
        if not candidates:
            return None

        # 优先挑「状态机明确标记需要恢复」的那条，否则取最后一条
        # （同一个 run 里前面的失败可能已经被后续步骤覆盖了）
        candidates.sort(
            key=lambda r: (r.status == ExecutionStatus.RECOVERY_REQUIRED, r.id or 0)
        )
        record = candidates[-1]
        return PendingToolCall(
            call_id=record.call_id,
            idempotency_key=record.idempotency_key,
            tool_name=record.tool_name,
            arguments=dict(record.arguments or {}),
            attempt=record.attempt,
        )

    @staticmethod
    def _idempotency_status_of(
        submit: SubmitResult, key: Optional[str]
    ) -> Optional[IdempotencyStatus]:
        """从 ``SubmitResult`` 反推幂等状态（§8）。

        为什么需要反推：``SubmitResult`` 是**面向 Agent** 的契约，
        它说的是「这次调用怎么样了」（``ExecutionStatus``），
        而不是「幂等键上记的是什么」（``IdempotencyStatus``）。
        恢复流程关心的是后者 —— 它决定「这笔操作做过没有」。
        两者大体同构，映射如下：

        =========================== =====================
        ExecutionStatus             IdempotencyStatus
        =========================== =====================
        SUCCESS / COMPLETED         SUCCESS
        PROCESSING / QUEUED / ...   PROCESSING
        FAILED                      FAILED
        =========================== =====================

        除此之外还看两个更直接的证据：``deduplicated``（幂等命中才会为真）
        与 ``detail["idempotency_status"]``（网关愿意直接告诉我们时最准）。
        """
        detail = submit.detail or {}
        raw = detail.get("idempotency_status")
        if raw:
            try:
                return IdempotencyStatus(str(raw))
            except ValueError:
                pass

        if submit.deduplicated:
            return IdempotencyStatus.SUCCESS
        if submit.is_final:
            return IdempotencyStatus.SUCCESS
        if submit.status == ExecutionStatus.FAILED:
            return IdempotencyStatus.FAILED
        return IdempotencyStatus.PROCESSING if key else None

    def _rebuild_call(self, plan: ResumePlan) -> ToolCall:
        """按原参数、原幂等键重建提交请求。

        幂等键**显式写死**成 :attr:`ResumePlan.idempotency_key`，
        而不是让 :meth:`ToolCall.ensure_idempotency_key` 现算：
        两者理论上应当相等（§7 的四个分量都没变），
        但「理论上相等」不足以保证线上相等 —— 只要有一处（比如
        ``logical_step_id`` 被谁改写了一位）对不上，
        平台就会把它当成一笔全新的调用，于是重复执行。
        直接把键钉死，让这种可能不存在的差异也一并消失。
        """
        if not plan.idempotency_key:
            # 理论上不该发生（plan_resume 一定带上幂等键），
            # 但真发生了就必须现算一个，否则提交上去会是一笔「没有防重保护」的调用。
            # logical_step_id 用默认值：这个分量只影响**同一 run 内**的区分，
            # 而恢复场景下我们本来就是在重放同一个步骤。
            fallback = ToolCall(
                tool_name=plan.tool_name or "",
                arguments=dict(plan.arguments or {}),
                tenant_id=plan.tenant_id or "tenantA",
                graph_run_id=plan.run_id or "run_001",
                attempt=plan.attempt + 1,
            )
            fallback.ensure_idempotency_key()
            return fallback

        call = ToolCall(
            tool_name=plan.tool_name or "",
            arguments=dict(plan.arguments or {}),
            tenant_id=plan.tenant_id or "tenantA",
            graph_run_id=plan.run_id or "run_001",
            idempotency_key=plan.idempotency_key,
            attempt=plan.attempt + 1,
        )
        return call

    def _audit(self, plan: ResumePlan, *, outcome: str, extra: Optional[dict] = None) -> None:
        """往 ``execution_event`` 追加 ``agent.resumed``（§42）。

        审计写入**不允许**影响恢复流程本身：事件表是 append-only 的旁路，
        写失败最多丢一条日志，绝不能让「恢复」因为「记不上账」而失败。
        """
        if self.db is None or not plan.call_id:
            return
        payload = {"outcome": outcome, **plan.to_dict()}
        if extra:
            payload["extra"] = extra
        try:
            self.db.append_event(plan.call_id, "agent.resumed", payload)
        except Exception as exc:  # pragma: no cover
            logger.warning("写 agent.resumed 事件失败 call=%s: %s", plan.call_id, exc)


# ======================================================================
def _submit_view(submit: SubmitResult) -> dict:
    """SubmitResult -> 可打印 dict（把 ResultEnvelope 压成 Agent 视图）。"""
    view = submit.model_dump(mode="json", exclude={"result"})
    view["result"] = submit.result.to_agent_view() if submit.result else None
    return view
