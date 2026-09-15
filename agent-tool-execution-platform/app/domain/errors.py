"""平台异常层次 —— 每个异常**自带** :class:`ErrorType` 分类。

设计要点（说明书 §13 / §34）：
``FAILED`` 不能简单理解成 ``FAILED -> retry``。异常在抛出点就知道自己属于哪一类，
因此把分类信息挂在异常实例上，由 :mod:`app.recovery.classifier` 读出并交给
Recovery Policy 决策，避免「在错误处理里反推错误类型」这种脆弱写法。
"""
from __future__ import annotations

from .enums import ErrorType, RecoveryAction


class ToolPlatformError(Exception):
    """平台异常基类。

    :param message: 面向人的描述
    :param error_type: §34 错误分类
    :param retryable: 该类错误是否允许重试（Recovery Policy 的输入之一）
    :param detail: 结构化附加信息，会写进 ``execution_event.payload``
    """

    error_type: ErrorType = ErrorType.INTERNAL_ERROR
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        error_type: ErrorType | None = None,
        retryable: bool | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if error_type is not None:
            self.error_type = error_type
        if retryable is not None:
            self.retryable = retryable
        self.detail: dict = detail or {}

    def to_dict(self) -> dict:
        """序列化进 DB / Redis / 事件流。"""
        return {
            "type": type(self).__name__,
            "error_type": self.error_type.value,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"[{self.error_type.value}] {self.message}"


# ---------------------------------------------------------------- 校验 / 安全
class ValidationError(ToolPlatformError):
    """Schema 校验失败 —— 交给参数自愈流程（§22），不是直接失败。"""

    error_type = ErrorType.VALIDATION_ERROR
    retryable = False


class ParameterRepairError(ToolPlatformError):
    """参数自愈尝试过但修不好：LLM 修复 + 确定性校验都没过（§23）。"""

    error_type = ErrorType.VALIDATION_ERROR
    retryable = False


class InjectionDetected(ToolPlatformError):
    """§24 参数注入防护命中：路径穿越 / 命令注入 / SQL / SSRF / Prompt 注入。

    属于安全事件，**绝不重试**，且必须留下审计事件。
    """

    error_type = ErrorType.VALIDATION_ERROR
    retryable = False


class PermissionDenied(ToolPlatformError):
    """§26 权限不足 —— 直接拒绝，不 retry。"""

    error_type = ErrorType.PERMISSION_ERROR
    retryable = False


class ToolNotFound(ToolPlatformError):
    """§35 Tool Not Found -> Fallback（可能换等价 Tool）。"""

    error_type = ErrorType.TOOL_NOT_FOUND
    retryable = False


# ------------------------------------------------------------------ 执行期
class ToolTimeout(ToolPlatformError):
    """Tool Timeout -> Retry / Increase timeout（§35）。"""

    error_type = ErrorType.TIMEOUT
    retryable = True


class NetworkError(ToolPlatformError):
    """Network Error -> Retry + Backoff（§35）。"""

    error_type = ErrorType.NETWORK_ERROR
    retryable = True


class ResourceExhausted(ToolPlatformError):
    """Resource Exhausted -> 降低资源/换节点（§35）。"""

    error_type = ErrorType.RESOURCE_EXHAUSTED
    retryable = True


class SandboxError(ToolPlatformError):
    """Sandbox Error -> 新建 Sandbox（§35）。"""

    error_type = ErrorType.SANDBOX_ERROR
    retryable = True


class BusinessError(ToolPlatformError):
    """业务逻辑失败 —— 是否重试由 Tool Policy 决定，默认不重试（§13）。"""

    error_type = ErrorType.BUSINESS_ERROR
    retryable = False


class InternalError(ToolPlatformError):
    """平台自身缺陷。"""

    error_type = ErrorType.INTERNAL_ERROR
    retryable = False


# ---------------------------------------------------------------- 可靠性控制面
class LeaseLost(ToolPlatformError):
    """续租时 CAS 失败：本 Worker 的 lease_id 已不是当前持有者（§15）。

    典型场景：Worker A 断网 → Reaper 判死 → Worker B 接管 → A 恢复后想续租。
    此时 A **必须**停止写结果，否则会覆盖 B 的成果。
    """

    error_type = ErrorType.INTERNAL_ERROR
    retryable = False


class DuplicateExecution(ToolPlatformError):
    """§30 重复执行检测：同 tool + 同参数 + 同图步 + 短时间窗内连续重复。"""

    error_type = ErrorType.INTERNAL_ERROR
    retryable = False


class CycleDetected(ToolPlatformError):
    """§31 DAG 循环检测：``A -> B -> A -> B`` 且超过阈值。"""

    error_type = ErrorType.INTERNAL_ERROR
    retryable = False


class Cancelled(ToolPlatformError):
    """§41 PROCESSING -> CANCELLING -> CANCELLED。"""

    error_type = ErrorType.INTERNAL_ERROR
    retryable = False


class HumanRejected(ToolPlatformError):
    """§52 人工审批 REJECT -> CANCELLED。"""

    error_type = ErrorType.PERMISSION_ERROR
    retryable = False


# 异常 -> RecoveryAction 的兜底映射。
# 注意：这里给的是「异常自身的倾向」，真正的决策在 recovery/policy.py，
# 因为它还要结合 attempt 次数、Tool 的 idempotency_level、risk_level。
DEFAULT_ACTION_BY_ERROR: dict[ErrorType, RecoveryAction] = {
    ErrorType.VALIDATION_ERROR: RecoveryAction.REPAIR,
    ErrorType.PERMISSION_ERROR: RecoveryAction.ABORT,
    ErrorType.TIMEOUT: RecoveryAction.RETRY,
    ErrorType.NETWORK_ERROR: RecoveryAction.RETRY,
    ErrorType.RESOURCE_EXHAUSTED: RecoveryAction.RETRY,
    ErrorType.TOOL_NOT_FOUND: RecoveryAction.FALLBACK,
    ErrorType.BUSINESS_ERROR: RecoveryAction.ABORT,
    ErrorType.SANDBOX_ERROR: RecoveryAction.RETRY,
    ErrorType.INTERNAL_ERROR: RecoveryAction.ABORT,
}
