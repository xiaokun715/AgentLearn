"""平台配置 —— ``configs/*.yaml`` + 环境变量 + 代码默认值 三层合并。

优先级（后者覆盖前者）：**代码默认值 < YAML < 环境变量**。

对应说明书：
- §59 Tool Registry 的 YAML 声明（``configs/tools.yaml``）
- §27 Sandbox 资源策略（``configs/sandbox.yaml``）
- §35/§36 恢复与重试策略（``configs/recovery.yaml``）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .domain.policy import (
    LoopPolicy,
    ParamPolicy,
    RecoveryPolicyConfig,
    RetryPolicy,
    SandboxPolicy,
)

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _load_yaml(path: Path) -> dict[str, Any]:
    """读一个 YAML 文件；不存在或为空返回 ``{}``（便于零配置启动）。"""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class ToolOverride(BaseModel):
    """``configs/tools.yaml`` 里对某个 Tool 的策略覆盖（§59）。

    代码里声明的 ``ToolMetadata`` 是**默认值**，YAML 只覆盖需要调的部分 ——
    这样新增 Tool 不必先写 YAML，运维调参也不必改代码。
    """

    model_config = ConfigDict(extra="forbid")

    version: Optional[str] = None
    mode: Optional[str] = None
    estimated_duration_ms: Optional[int] = None
    timeout_ms: Optional[int] = None
    risk: Optional[str] = None
    fallback_tool: Optional[str] = None
    idempotency_level: Optional[str] = None

    permissions: Optional[list[str]] = None

    retry: Optional[RetryPolicy] = None
    sandbox: Optional[SandboxPolicy] = None

    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """§25 逐参数策略：``params.path.allowed_prefix`` 等。"""

    allow_llm_repair: Optional[bool] = None
    injection_guard: Optional[bool] = None

    def param_policy(self) -> ParamPolicy:
        return ParamPolicy(
            rules=self.params,
            allow_llm_repair=(
                self.allow_llm_repair if self.allow_llm_repair is not None else True
            ),
            injection_guard=(
                self.injection_guard if self.injection_guard is not None else True
            ),
        )


class AppConfig(BaseModel):
    """整个平台的运行时配置。"""

    model_config = ConfigDict(extra="forbid")

    # ---- 存储 ----
    db_path: str = ":memory:"
    artifact_root: str = "artifacts"

    # ---- 沙箱 ----
    sandbox_backend: str = "auto"
    default_sandbox: SandboxPolicy = Field(default_factory=SandboxPolicy)
    workspace_root: str = "workspaces"

    # ---- 可靠性 ----
    default_retry: RetryPolicy = Field(default_factory=RetryPolicy)
    loop: LoopPolicy = Field(default_factory=LoopPolicy)
    recovery: RecoveryPolicyConfig = Field(default_factory=RecoveryPolicyConfig)

    # ---- 幂等 / 租约 ----
    idempotency_ttl_seconds: int = 24 * 3600
    """幂等键存活时间。要**长于** Agent 崩溃恢复的最坏耗时，否则恢复时会「查不到」而重跑。"""

    idempotency_claim_grace_seconds: float = 300.0
    """**认领宽限期**：幂等键被认领后、尚无租约的这段窗口内一律 WAIT。

    为什么需要它：§11 的分支图隐含「PROCESSING 必定有租约持有者」，
    这在同步路径成立（submit 当场抢租约），在异步路径**不成立** ——
    Gateway 认领幂等键时任务还只是躺在队列里，抢租约的是稍后来的 Worker。
    没有这层宽限，第二个相同幂等键的请求会把「正在排队」误判为「执行者已死」
    并放行第二次执行，恰好击穿幂等（§71 Test 1）。

    取值要**大于最坏排队时间**（含 §36 退避重试的累计等待），
    否则正常排队中的任务会被误判为死亡。默认 300s 对应「队列积压 5 分钟仍属正常」。
    """

    lock_ttl_seconds: int = 30
    lease_ttl_seconds: int = 30
    heartbeat_interval_seconds: float = 10.0
    """§15：``Lease TTL = 30s``，``Heartbeat = 10s`` -> 允许连丢两次心跳。"""

    agent_heartbeat_interval_seconds: float = 10.0
    agent_heartbeat_ttl_seconds: int = 60
    """§16/§17 Agent 心跳：判活 Agent / Workflow，决定 Tool 是继续跑、取消还是转人工。"""

    lease_reap_interval_seconds: float = 5.0

    # ---- 结果处理 ----
    max_inline_size: int = 32 * 1024
    """§40 ``max_inline_size = 32KB``：超过就走 artifact。"""

    preview_head_size: int = 8 * 1024
    preview_tail_size: int = 8 * 1024
    """§40：返回「前 8KB + 后 8KB + 统计信息 + artifact_id」。"""

    # ---- 调度 ----
    max_concurrency: int = 4
    worker_poll_interval_seconds: float = 0.05
    worker_count: int = 2

    # ---- 观测 ----
    metrics_enabled: bool = True
    trace_enabled: bool = True

    # ---- Tool 覆盖 ----
    tools: dict[str, ToolOverride] = Field(default_factory=dict)

    # ---- 权限（§26）----
    permissions: dict[str, list[str]] = Field(default_factory=dict)
    """``principal -> [action, ...]``；缺省时由 :mod:`app.gateway.permission` 兜底。"""

    # ==================================================================
    @classmethod
    def load(cls, config_dir: Optional[str | Path] = None) -> "AppConfig":
        """从 ``configs/`` 组装配置，再叠加环境变量覆盖。"""
        directory = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR

        sandbox_yaml = _load_yaml(directory / "sandbox.yaml")
        recovery_yaml = _load_yaml(directory / "recovery.yaml")
        tools_yaml = _load_yaml(directory / "tools.yaml")

        payload: dict[str, Any] = {}

        # --- sandbox.yaml ---
        if "default" in sandbox_yaml:
            payload["default_sandbox"] = sandbox_yaml["default"]
        if "workspace_root" in sandbox_yaml:
            payload["workspace_root"] = sandbox_yaml["workspace_root"]
        if "backend" in sandbox_yaml:
            payload["sandbox_backend"] = sandbox_yaml["backend"]

        # --- recovery.yaml ---
        if "retry" in recovery_yaml:
            payload["default_retry"] = recovery_yaml["retry"]
        if "loop" in recovery_yaml:
            payload["loop"] = recovery_yaml["loop"]
        if "recovery" in recovery_yaml:
            payload["recovery"] = recovery_yaml["recovery"]

        # --- tools.yaml ---
        raw_tools = tools_yaml.get("tools", {}) or {}
        payload["tools"] = raw_tools
        if "permissions" in tools_yaml:
            payload["permissions"] = tools_yaml["permissions"]

        # --- 环境变量（最高优先级）---
        payload["db_path"] = os.getenv("TOOLPLAT_DB_PATH", ":memory:")
        payload["artifact_root"] = os.getenv("TOOLPLAT_ARTIFACT_ROOT", "artifacts")
        payload["sandbox_backend"] = os.getenv(
            "TOOLPLAT_SANDBOX_BACKEND", payload.get("sandbox_backend", "auto")
        )
        payload["lease_ttl_seconds"] = _env_int(
            "TOOLPLAT_LEASE_TTL_SECONDS", cls.model_fields["lease_ttl_seconds"].default
        )
        payload["idempotency_ttl_seconds"] = _env_int(
            "TOOLPLAT_IDEMPOTENCY_TTL_SECONDS",
            cls.model_fields["idempotency_ttl_seconds"].default,
        )
        payload["max_inline_size"] = _env_int(
            "TOOLPLAT_MAX_INLINE_SIZE", cls.model_fields["max_inline_size"].default
        )
        payload["worker_count"] = _env_int(
            "TOOLPLAT_WORKER_COUNT", cls.model_fields["worker_count"].default
        )
        payload["metrics_enabled"] = _env_bool("TOOLPLAT_METRICS_ENABLED", True)

        return cls.model_validate(payload)

    @classmethod
    def for_demo(cls, **overrides: Any) -> "AppConfig":
        """演示/推演用的紧凑配置：内存 DB、子进程沙箱、快心跳。

        **先读 ``configs/*.yaml`` 再叠加演示期覆盖** —— 这一点很重要：
        Tool 的逐参数安全策略（§25 的 ``allowed_prefix`` / ``allowed`` / 区间）
        全部写在 ``configs/tools.yaml`` 里，绕过它就等于关掉了一半的安全演示。
        演示期只覆盖「时间尺度」相关的量（心跳、租约、TTL），让
        「租约过期 -> Reaper 接管」这类场景几秒内就能跑完。

        :param overrides: 覆盖任意字段，例如 ``lease_ttl_seconds=5``。
        """
        payload: dict[str, Any] = cls.load().model_dump()
        payload.update(
            {
                "db_path": ":memory:",
                "sandbox_backend": "process",
                # 时间尺度压缩：生产是 30s TTL / 10s 心跳（§15），演示里按秒走
                "lease_ttl_seconds": 5,
                "heartbeat_interval_seconds": 1.0,
                "lease_reap_interval_seconds": 1.0,
                "idempotency_ttl_seconds": 3600,
                "worker_poll_interval_seconds": 0.02,
                # 演示里队列是空的，宽限期不需要生产那么长
                "idempotency_claim_grace_seconds": 30.0,
                "trace_enabled": True,
                "metrics_enabled": True,
            }
        )
        payload.update(overrides)
        return cls.model_validate(payload)

    def tool_override(self, name: str) -> Optional[ToolOverride]:
        return self.tools.get(name)

    def effective_retry(self, name: str) -> RetryPolicy:
        override = self.tool_override(name)
        if override is not None and override.retry is not None:
            return override.retry
        return self.default_retry

    def effective_sandbox(self, name: str) -> SandboxPolicy:
        override = self.tool_override(name)
        if override is not None and override.sandbox is not None:
            return override.sandbox
        return self.default_sandbox

    def effective_param_policy(self, name: str) -> ParamPolicy:
        override = self.tool_override(name)
        if override is None:
            return ParamPolicy()
        return override.param_policy()
