"""策略值对象 —— 说明书 §25 / §27 / §35 / §36 的可配置部分。

放在 ``domain`` 而不是各自的子包里，是为了打断循环依赖：
Tool Registry 需要「沙箱策略 / 重试策略 / 参数策略」才能组装出一个完整 ToolSpec，
而 Gateway 又需要 Registry 才能拿到参数模型。策略本身**不依赖任何子系统**，
于是下沉到 domain，两边都能引。

配置来源见 ``configs/*.yaml``：代码里写默认值，YAML 覆盖。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .enums import ErrorType, RecoveryAction


class ParamRule(BaseModel):
    """单个参数的**确定性**安全约束（§25）。

    ::

        run_test:
          path:
            type: string
            allowed_prefix: [/workspace/tests/]

          command:
            allowed: [pytest, python]

          network:
            allowed: false

    关键点：这些规则在**参数自愈之后**执行（§23 的 ``Schema -> Business Constraints ->
    Permission Policy`` 三段式）。LLM 可以帮忙把 ``"300"`` 修成 ``300``，
    但 ``timeout`` 能否等于 ``300000`` 由这里的区间说了算 ——
    绝不允许「LLM Repair Only」。
    """

    model_config = ConfigDict(extra="forbid")

    type: Optional[Literal["string", "integer", "number", "boolean", "array", "object"]] = None

    # 取值范围（数值型）
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    # 长度约束（字符串/数组）
    min_length: Optional[int] = None
    max_length: Optional[int] = None

    # 白名单：字符串用枚举、列表用「每个元素都必须在其中」
    allowed: Optional[list[Any]] = None

    # 路径类参数：必须落在这些前缀之下（§24 路径穿越）
    allowed_prefix: Optional[list[str]] = None
    denied_prefix: Optional[list[str]] = None

    # 正则白名单（如 test_case 只允许 TC\\d{3}）
    pattern: Optional[str] = None

    # 该参数是否禁止出现注入特征
    injection_check: bool = True

    # 业务语义：即使数值合法也不许被自愈放大（§23）
    repair_forbidden: bool = False

    def describe(self) -> str:
        """给 LLM 修复提示词用的人类可读约束描述。"""
        bits: list[str] = []
        if self.type:
            bits.append(f"type={self.type}")
        if self.minimum is not None or self.maximum is not None:
            bits.append(f"range=[{self.minimum}, {self.maximum}]")
        if self.allowed is not None:
            bits.append(f"allowed={self.allowed}")
        if self.allowed_prefix:
            bits.append(f"path_prefix={self.allowed_prefix}")
        if self.pattern:
            bits.append(f"pattern={self.pattern}")
        if self.max_length is not None:
            bits.append(f"max_length={self.max_length}")
        return ", ".join(bits) or "no extra constraint"


class ParamPolicy(BaseModel):
    """一个 Tool 的全部参数安全策略（§25）。"""

    model_config = ConfigDict(extra="forbid")

    rules: dict[str, ParamRule] = Field(default_factory=dict)

    # 是否允许 LLM 参与参数自愈；关掉则只做确定性修复
    allow_llm_repair: bool = True

    # 注入检测的总开关（安全基线，默认开）
    injection_guard: bool = True

    def rule_for(self, name: str) -> Optional[ParamRule]:
        return self.rules.get(name)

    def apply(self, values: dict[str, Any]) -> dict[str, Any]:
        """把本策略的规则并进一个 dict（YAML 覆盖用）。"""
        merged = dict(values)
        for key, rule in self.rules.items():
            merged[key] = rule.model_dump()
        return merged


class SandboxPolicy(BaseModel):
    """§27 Sandbox 资源限制。"""

    model_config = ConfigDict(extra="forbid")

    cpu: float = 2.0
    memory_mb: int = 2048
    disk_mb: int = 4096
    timeout_seconds: float = 300.0
    network: bool = False
    max_processes: int = 20

    # 允许写入的目录白名单；空表示使用 workspace 默认
    writable_paths: list[str] = Field(default_factory=list)
    # 只读挂载（如 /workspace/tests）
    readonly_paths: list[str] = Field(default_factory=list)

    backend: Literal["auto", "docker", "process"] = "auto"


class RetryPolicy(BaseModel):
    """§36 Retry Policy：Exponential Backoff + Jitter。"""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = 3
    initial_backoff_ms: int = 1000
    max_backoff_ms: int = 30_000
    multiplier: float = 2.0
    jitter_ratio: float = 0.3
    """抖动比例。加抖动是为了避免「大量 Agent 同时 Retry -> 服务雪崩」（§36）。"""

    retry_on: list[ErrorType] = Field(
        default_factory=lambda: [
            ErrorType.TIMEOUT,
            ErrorType.NETWORK_ERROR,
            ErrorType.RESOURCE_EXHAUSTED,
            ErrorType.SANDBOX_ERROR,
        ]
    )

    def allows(self, error_type: ErrorType) -> bool:
        return error_type in self.retry_on


class LoopPolicy(BaseModel):
    """§30/§31/§32 重复与循环检测阈值。"""

    model_config = ConfigDict(extra="forbid")

    window_seconds: float = 60.0
    """「短时间窗」—— 超出这个窗口的相同调用不算重复。"""

    duplicate_warn: int = 2
    duplicate_degrade: int = 3
    duplicate_stop: int = 5

    cycle_warn: int = 2
    cycle_degrade: int = 3
    cycle_stop: int = 4

    max_execution_path: int = 200
    """执行路径的保留长度（§31 ``execution_path``），防止无限增长。"""


class RecoveryPolicyConfig(BaseModel):
    """§35 Recovery Policy 表：错误类型 -> 处置动作。

    说明书给的是「表格」，这里就把它落成一张**可覆盖的表**——
    每个 Tool 可以在 YAML 里改自己那一行的动作，而不必改代码。
    """

    model_config = ConfigDict(extra="forbid")

    actions: dict[ErrorType, RecoveryAction] = Field(
        default_factory=lambda: {
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
    )

    # 租约过期且执行状态不确定时的处置（§56）：high 强制人工
    uncertain_high_risk_action: RecoveryAction = RecoveryAction.HUMAN
    uncertain_low_risk_action: RecoveryAction = RecoveryAction.RETRY

    def action_for(self, error_type: ErrorType) -> RecoveryAction:
        return self.actions.get(error_type, RecoveryAction.ABORT)
