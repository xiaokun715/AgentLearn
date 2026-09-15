"""平台枚举：状态机、错误类型、幂等等级、恢复动作。

对齐《第十二章：可靠工具执行系统设计说明书》：
- §41 Tool Execution 状态机  -> :class:`ExecutionStatus`
- §8  Idempotency 状态机      -> :class:`IdempotencyStatus`
- §34 Error Classification    -> :class:`ErrorType`
- §35 Recovery Policy         -> :class:`RecoveryAction`
- §57 Tool 的幂等性等级        -> :class:`IdempotencyLevel`
- §32 循环检测策略             -> :class:`LoopSignal`
"""
from __future__ import annotations

from enum import Enum


class ExecutionMode(str, Enum):
    """§4 Tool 按执行模型分类：短任务直等，长任务进队列。"""

    SYNC = "sync"
    ASYNC = "async"


class RiskLevel(str, Enum):
    """§51 风险等级：HIGH 直接进人工审批。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IdempotencyLevel(str, Enum):
    """§57 幂等性等级 —— 决定 Crash 之后能不能直接重跑。

    ==================== ================== ====================================
    等级                  典型 Tool          Crash 后策略
    ==================== ================== ====================================
    PURE                  calculator         可重试
    IDEMPOTENT            run_test           可重试
    AT_LEAST_ONCE         message publish    先查询状态，再决定
    NON_IDEMPOTENT        payment            人工确认 / 外部事务号
    ==================== ================== ====================================
    """

    PURE = "pure"
    IDEMPOTENT = "idempotent"
    AT_LEAST_ONCE = "at_least_once"
    NON_IDEMPOTENT = "non_idempotent"

    @property
    def crash_safe_to_retry(self) -> bool:
        """租约过期后是否允许新 Worker 直接接管重跑（§56）。"""
        return self in (IdempotencyLevel.PURE, IdempotencyLevel.IDEMPOTENT)


class ExecutionStatus(str, Enum):
    """§41 Tool Execution 完整状态机。

    ::

        CREATED → VALIDATING → QUEUED → ACQUIRING → PROCESSING
                                            ├── SUCCESS ──► COMPLETED
                                            └── FAILED  ──► RECOVERING
                                                              ├── RETRY (回 QUEUED)
                                                              ├── FALLBACK
                                                              └── HUMAN → WAITING_HUMAN
        PROCESSING → CANCELLING → CANCELLED
        租约过期且执行状态不确定 → RECOVERY_REQUIRED（§56）
    """

    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    ACQUIRING = "ACQUIRING"
    PROCESSING = "PROCESSING"

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"

    RECOVERING = "RECOVERING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

    WAITING_HUMAN = "WAITING_HUMAN"

    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"

    COMPLETED = "COMPLETED"

    @property
    def is_terminal(self) -> bool:
        """终态：不会再发生状态迁移。"""
        return self in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.WAITING_HUMAN,
            ExecutionStatus.RECOVERY_REQUIRED,
        )

    @property
    def is_pending(self) -> bool:
        """Agent 视角的「还没出结果」：应当 WAIT / POLL 而不是重新提交。"""
        return self in (
            ExecutionStatus.CREATED,
            ExecutionStatus.VALIDATING,
            ExecutionStatus.QUEUED,
            ExecutionStatus.ACQUIRING,
            ExecutionStatus.PROCESSING,
            ExecutionStatus.RECOVERING,
            ExecutionStatus.CANCELLING,
        )


class IdempotencyStatus(str, Enum):
    """§8 Idempotency 状态机（Redis ``idempotency:{key}`` 里存的 status）。

    ::

        PROCESSING ──┬──► SUCCESS
                     └──► FAILED（再由 ErrorClassifier 细分去向 §13）
    """

    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ErrorType(str, Enum):
    """§34 Error Classification —— 错误不能统一处理，先分类再定策略。"""

    VALIDATION_ERROR = "validation_error"
    PERMISSION_ERROR = "permission_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    TOOL_NOT_FOUND = "tool_not_found"
    BUSINESS_ERROR = "business_error"
    SANDBOX_ERROR = "sandbox_error"
    INTERNAL_ERROR = "internal_error"

    def __str__(self) -> str:  # 便于日志直接打印
        return self.value


class RecoveryAction(str, Enum):
    """§35 Recovery Policy 的落点，也是 §41 状态机 RECOVERING 的分叉。"""

    RETRY = "RETRY"
    REPAIR = "REPAIR"
    FALLBACK = "FALLBACK"
    HUMAN = "HUMAN"
    ABORT = "ABORT"
    WAIT = "WAIT"  # PROCESSING + Lease 仍有效（§11）


class LoopSignal(str, Enum):
    """§32 循环检测策略：不是发现一次重复就停止，而是逐级升级。"""

    OK = "OK"
    WARNING = "WARNING"
    DEGRADED = "DEGRADED"
    STOP = "STOP"


class SubmitOutcome(str, Enum):
    """Tool Gateway 对一次 submit 给出的裁决 —— Agent 侧据此分支。"""

    EXECUTED = "EXECUTED"          # 同步执行完成，结果已在手
    ACCEPTED = "ACCEPTED"          # 异步已入队，等回调/轮询
    DEDUPLICATED = "DEDUPLICATED"  # 幂等命中 SUCCESS，直接复用结果（§12）
    WAITING = "WAITING"            # 幂等命中 PROCESSING 且租约有效（§11）
    WAITING_HUMAN = "WAITING_HUMAN"  # §51 高风险
    REJECTED = "REJECTED"          # 校验/权限/注入拦截，不可重试
    FAILED = "FAILED"              # 执行失败，携带分类后的错误
