"""领域异常。API 层把它们翻译成明确的 HTTP 错误。"""
from __future__ import annotations


class SandboxError(Exception):
    """沙箱领域错误基类。"""


class PolicyNotFound(SandboxError):
    pass


class PolicyDenied(SandboxError):
    """Policy Engine 判定 DENY。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ExecutionNotFound(SandboxError):
    pass


class InvalidToolType(SandboxError):
    pass


class SandboxBackendUnavailable(SandboxError):
    """沙箱后端不可用（Docker daemon 未启动 / 依赖未安装）。"""


class KillNotAllowed(SandboxError):
    """执行已终结，无法再 kill（幂等场景由调用方自行吞掉）。"""
