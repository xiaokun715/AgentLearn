"""恢复策略引擎 —— 说明书 §35 Recovery Policy（含 §75 的「不无限 Retry」保证）。

一句话职责
----------
**把「分类后的错误」翻译成「下一步动作」。** 上游是
:class:`~app.recovery.classifier.ErrorClassifier`，下游是 §41 状态机里
``RECOVERING`` 的那个分叉::

    RECOVERING ──┬── RETRY    -> 回 QUEUED（带 backoff）
                 ├── REPAIR   -> 参数自愈后重放（§22-§25）
                 ├── FALLBACK -> 换 fallback_tool
                 ├── HUMAN    -> WAITING_HUMAN（§51-§53）
                 └── ABORT    -> 终态失败（不再自动尝试）

§35 那张表是**基础动作**，但光有表不够
--------------------------------------
说明书的表只回答了「这类错误一般怎么办」。真实系统里还要回答三个问题，
本引擎用三条规则补上（顺序即优先级）：

* **规则 A（防无限 Retry）**：表说 ``RETRY``，但次数用尽或该错误不在
  ``retry_on`` 里 -> 降级 ``ABORT``。这是 §75 最后一句
  「Recovery Engine 保证异常情况下系统不会简单地无限 Retry」的落点：
  没有这道闸门，一次永久性故障会变成一场永不停止的重放。
* **规则 B（防悬空 Fallback）**：表说 ``FALLBACK``，但这个 Tool 根本没配
  ``fallback_tool`` -> 降级 ``ABORT``。指向一个不存在的替代 Tool，
  只会把失败推迟到下一次报错，还会污染审计链路。
* **规则 C（高风险转人工）**：§35 表格最后一行「高风险错误 -> Human」。
  风险高的操作（``database_delete`` 之类）即使错误本身可重试，
  也不能由平台自己决定再跑一遍 —— 副作用不可撤销。

另外：``attempt >= max_attempts`` 时为终局 ``ABORT``（**无论如何**），
它排在最后，保证任何规则都不能绕开「次数用尽」这个硬边界。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import AppConfig
from ..domain.enums import ErrorType, RecoveryAction, RiskLevel
from ..domain.errors import ToolNotFound
from ..domain.policy import RetryPolicy
from ..tools.registry import ToolRegistry
from .classifier import ErrorClassifier
from .retry import RetryController


@dataclass
class RecoveryDecision:
    """一次恢复决策的完整结论 —— 状态机照着 ``action`` 走，日志照着 ``reason`` 读。

    :param action: 下一步动作（§35）
    :param error_type: 触发本次决策的错误分类（§34）
    :param attempt: 已经失败了几次（0 表示这是第一次失败）
    :param max_attempts: 该 Tool 允许的总尝试次数（来自它的 :class:`RetryPolicy`）
    :param backoff_ms: 若动作是 ``RETRY``，下次执行前应等待的毫秒数
    :param reason: 一句可以直接念出来的中文，说明「为什么是这个动作」
    :param fallback_tool: ``FALLBACK`` 时的替代 Tool
    :param escalate_to_human: 是否需要真正拉起人工审批单（§51）
    :param detail: 结构化附加信息，写进 ``execution_event.payload``
    """

    action: RecoveryAction
    error_type: ErrorType
    attempt: int
    max_attempts: int
    backoff_ms: int
    reason: str
    fallback_tool: Optional[str] = None
    escalate_to_human: bool = False
    detail: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """是否需要停止自动推进（人工与放弃都算「平台不再自己往前走」）。"""
        return self.action in (RecoveryAction.ABORT, RecoveryAction.HUMAN)

    def to_dict(self) -> dict:
        """落库 / 打日志用的 JSON-safe 形态。"""
        return {
            "action": self.action.value,
            "error_type": self.error_type.value,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "backoff_ms": self.backoff_ms,
            "reason": self.reason,
            "fallback_tool": self.fallback_tool,
            "escalate_to_human": self.escalate_to_human,
            "detail": self.detail,
        }


class RecoveryPolicyEngine:
    """§35 恢复策略引擎。

    :param config: 平台配置，``config.recovery`` 提供「错误 -> 动作」那张表
    :param registry: Tool 注册表 —— 决策必须知道**是哪个 Tool 失败了**，
        因为 ``max_attempts`` / ``retry_on`` / ``fallback_tool`` / 风险等级
        都是 Tool 自己的属性（§5 / §59）
    :param retry: 退避与次数裁决器；缺省新建一个（生产用真随机抖动）
    """

    def __init__(
        self,
        config: AppConfig,
        registry: ToolRegistry,
        *,
        retry: Optional[RetryController] = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.retry = retry or RetryController()
        self.classifier = ErrorClassifier()

    # ==================================================================
    # 主入口
    # ==================================================================
    def decide(
        self,
        *,
        error_type: ErrorType,
        tool_name: str,
        attempt: int = 0,
        risk_level: Optional[RiskLevel] = None,
    ) -> RecoveryDecision:
        """给出这个错误该走哪条路（§35）。

        :param error_type: §34 分类结果
        :param tool_name: 哪个 Tool 失败了（未知 Tool 按「没有 fallback」处理）
        :param attempt: 已经失败了几次
        :param risk_level: 调用点已知的风险等级；不传则取 Tool 元数据里的声明
            （高风险 Tool 的声明在 registry 里，见 §51）
        """
        spec = self._spec_or_none(tool_name)
        metadata = spec.metadata if spec is not None else None
        policy: RetryPolicy = (
            spec.retry if spec is not None else self.config.effective_retry(tool_name)
        )

        # 风险等级的优先级：调用点显式传入 > Tool 元数据声明 > 未知即低风险
        effective_risk = risk_level
        if effective_risk is None and metadata is not None:
            effective_risk = metadata.risk_level
        if effective_risk is None:
            effective_risk = RiskLevel.LOW

        fallback_tool = metadata.fallback_tool if metadata is not None else None
        known_tool = spec is not None

        # --- 第 2 步：基础动作来自 configs/recovery.yaml 那张表 ---
        action = self.config.recovery.action_for(error_type)
        note = f"{error_type.value} 按策略表映射为 {action.value}"

        # --- 规则 A：RETRY 但不能再试 -> ABORT（§75 的防无限 Retry 闸门）---
        if action is RecoveryAction.RETRY:
            retryable = self.retry.should_retry(
                attempt=attempt, policy=policy, error_type=error_type
            )
            if not retryable:
                action = RecoveryAction.ABORT
                if attempt + 1 >= policy.max_attempts:
                    note = (
                        f"{error_type.value} 本可重试，但已用满 {policy.max_attempts} 次尝试"
                        f"（attempt={attempt}）-> ABORT，不再无限重试"
                    )
                else:
                    note = (
                        f"{error_type.value} 不在 {tool_name} 的 retry_on "
                        f"{[e.value for e in policy.retry_on]} 内 -> ABORT"
                    )

        # --- 规则 B：FALLBACK 但没配替代 Tool -> ABORT（别指向空气）---
        if action is RecoveryAction.FALLBACK and not fallback_tool:
            action = RecoveryAction.ABORT
            note = (
                f"{error_type.value} 需要 Fallback，但 "
                f"{'未知 Tool' if not known_tool else tool_name} 未配置 fallback_tool -> ABORT"
            )

        # --- 规则 C：高风险 + 非参数错误 -> HUMAN（§35 最后一行）---
        # 只在动作还是「平台会自己继续推进」时才升级：ABORT 已经是「不自动执行」的
        # 终局裁决，把权限错误升级成人工审批等于请人去推翻安全策略；
        # VALIDATION_ERROR 被排除，是因为修参数不产生副作用，不需要人点头。
        if (
            effective_risk is RiskLevel.HIGH
            and error_type is not ErrorType.VALIDATION_ERROR
            and action not in (RecoveryAction.ABORT, RecoveryAction.HUMAN)
        ):
            action = RecoveryAction.HUMAN
            note = (
                f"{error_type.value} 且风险等级为 HIGH -> 转人工审批"
                f"（§35 高风险错误 -> Human）"
            )

        # --- 第 6 步：次数用尽，无论如何 ABORT（硬边界，任何规则都绕不开）---
        if attempt >= policy.max_attempts and action is not RecoveryAction.ABORT:
            action = RecoveryAction.ABORT
            note = (
                f"attempt={attempt} 已达 max_attempts={policy.max_attempts} -> ABORT（硬边界）"
            )

        # --- 第 7 步：退避值（只有 RETRY 真正用得上，但始终填，便于统一落库）---
        backoff_ms = self.retry.backoff_ms(attempt, policy)

        escalate = action is RecoveryAction.HUMAN
        next_backoff = backoff_ms if action is RecoveryAction.RETRY else 0

        return RecoveryDecision(
            action=action,
            error_type=error_type,
            attempt=attempt,
            max_attempts=policy.max_attempts,
            backoff_ms=next_backoff,
            reason=self._compose_reason(
                action=action,
                error_type=error_type,
                tool_name=tool_name,
                attempt=attempt,
                policy=policy,
                backoff_ms=next_backoff,
                fallback_tool=fallback_tool,
                note=note,
            ),
            fallback_tool=fallback_tool if action is RecoveryAction.FALLBACK else None,
            escalate_to_human=escalate,
            detail={
                "tool_name": tool_name,
                "known_tool": known_tool,
                "risk_level": effective_risk.value,
                "retry_on": [e.value for e in policy.retry_on],
                "rule_note": note,
                "retry_plan": self.retry.describe(attempt, policy),
            },
        )

    def decide_for_exception(
        self,
        exc: BaseException,
        *,
        tool_name: str,
        attempt: int = 0,
    ) -> RecoveryDecision:
        """异常直达决策 —— 先 :meth:`ErrorClassifier.classify` 再 :meth:`decide`。

        额外一条守卫：平台异常自带 ``retryable`` 声明，当它为 ``False`` 时
        **不允许**落到 ``RETRY``。抛出点写 ``retryable=False`` 的人一定比
        错误类型表更了解这次失败（例如自定义的 ``ToolTimeout(retryable=False)``
        表示「超时但重放会重复扣款」），这条声明必须被尊重。
        """
        error_type = self.classifier.classify(exc)
        decision = self.decide(
            error_type=error_type, tool_name=tool_name, attempt=attempt
        )

        retryable_flag = getattr(exc, "retryable", None)
        if (
            retryable_flag is False
            and decision.action is RecoveryAction.RETRY
            and isinstance(exc, Exception)
        ):
            decision.action = RecoveryAction.ABORT
            decision.backoff_ms = 0
            decision.detail["rule_note"] = (
                f"异常 {type(exc).__name__} 声明 retryable=False -> 覆盖策略表，ABORT"
            )
            decision.reason = (
                f"异常 {type(exc).__name__} 自带 retryable=False（{error_type.value}），"
                f"重放不会改变结果 -> ABORT"
            )
        return decision

    # ==================================================================
    # 内部
    # ==================================================================
    def _spec_or_none(self, tool_name: str):
        """拿 ToolSpec；未注册返回 ``None``（**不抛异常**）。

        为什么这里吞掉 :class:`ToolNotFound`：恢复决策恰恰经常发生在
        「Tool 压根不存在」的场景里（§35 的 ``Tool Not Found -> Fallback``）。
        在恢复路径上抛异常会把一次可恢复的失败升级成平台崩溃。
        """
        try:
            return self.registry.get(tool_name)
        except ToolNotFound:
            return None

    @staticmethod
    def _compose_reason(
        *,
        action: RecoveryAction,
        error_type: ErrorType,
        tool_name: str,
        attempt: int,
        policy: RetryPolicy,
        backoff_ms: int,
        fallback_tool: Optional[str],
        note: str,
    ) -> str:
        """把决策拼成一句**可以直接读**的中文（运维不看代码就能懂）。"""
        if action is RecoveryAction.RETRY:
            return (
                f"{error_type.value} 属可重试错误，{tool_name} 第 {attempt + 1}/"
                f"{policy.max_attempts} 次尝试，退避 {backoff_ms}ms 后重试（{note}）"
            )
        if action is RecoveryAction.REPAIR:
            return (
                f"{error_type.value} 是参数问题，交给参数自愈（LLM 修复 + 确定性校验）"
                f"后重放，不做盲目重试（{note}）"
            )
        if action is RecoveryAction.FALLBACK:
            return (
                f"{error_type.value} 无法由 {tool_name} 完成，改用 fallback_tool="
                f"{fallback_tool} 继续（{note}）"
            )
        if action is RecoveryAction.HUMAN:
            return (
                f"{error_type.value} 且风险等级为 HIGH，{tool_name} 的副作用不可撤销，"
                f"转人工审批（§51），不再自动重试（{note}）"
            )
        return (
            f"{error_type.value} 不再自动推进：{note}；"
            f"{tool_name} 已尝试 {attempt + 1}/{policy.max_attempts} 次，转为终态失败"
        )
