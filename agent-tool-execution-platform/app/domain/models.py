"""平台领域模型 —— 全系统共享的数据契约。

对齐《第十二章：可靠工具执行系统设计说明书》：
- §5  Tool Metadata          -> :class:`ToolMetadata`
- §6  Tool Call 数据模型      -> :class:`ToolCall`
- §7  Idempotency Key        -> :func:`compute_idempotency_key`
- §8  Idempotency 状态        -> :class:`IdempotencyRecord`
- §14 Lease                  -> :class:`LeaseRecord`
- §18 Checkpoint 里的 pending -> :class:`PendingToolCall`
- §39/§40 大结果与截断        -> :class:`ResultEnvelope`
- §42 数据库设计              -> Execution/ToolResult/ExecutionEvent/OutboxEvent
- §52 Human Approval         -> :class:`ApprovalRecord`
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    ExecutionMode,
    ExecutionStatus,
    IdempotencyLevel,
    IdempotencyStatus,
    RecoveryAction,
    RiskLevel,
)


def utcnow() -> datetime:
    """带时区的当前时间（全平台统一时间源，便于测试注入）。"""
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    """生成 ``call_3f2a1b9c`` 形式的短 ID（说明书同款风格）。"""
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def new_call_id() -> str:
    return _new_id("call")


def new_result_id() -> str:
    return _new_id("result")


def new_artifact_id() -> str:
    return _new_id("artifact")


def new_lease_id() -> str:
    return _new_id("lease")


# ======================================================================
# §7 Idempotency Key
# ======================================================================
def normalize_arguments(arguments: dict) -> str:
    """把参数规范化成**稳定**字符串，消除「同一逻辑调用、写法不同」的差异。

    规则：键排序 + 紧凑分隔符 + 非 ASCII 不转义 + 丢弃值为 ``None`` 的键。

    为什么不能直接 ``hash(json.dumps(arguments))``：
    ``{"a": 1, "b": 2}`` 与 ``{"b": 2, "a": 1}`` 语义相同却哈希不同，
    会导致同一个逻辑操作拿到两个幂等键 —— 直接击穿防重能力。
    """
    cleaned = {k: v for k, v in arguments.items() if v is not None}
    return json.dumps(
        cleaned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def compute_idempotency_key(
    *,
    tenant_id: str,
    workflow_run_id: str,
    logical_step_id: str,
    tool_name: str,
    arguments: dict,
) -> str:
    """§7 生成稳定的 Idempotency Key。

    组成：``tenant_id + workflow_run_id + logical_step_id + tool_name + normalized_arguments``

    **为什么不能只 hash(arguments)**：两个不同任务可能恰好参数一样 ——
    比如两个用户都查 "TC001"，只 hash 参数会让第二个用户拿到第一个的结果。

    **为什么必须带 ``logical_step_id``**：同一个 run 里循环调用同一个 Tool（参数也相同）
    是合法意图（例如轮询），带上「逻辑步骤」才能把「同一步骤被重复提交」和
    「不同步骤各调一次」区分开 —— 前者要拦，后者要放。
    """
    material = "|".join(
        [
            tenant_id,
            workflow_run_id,
            logical_step_id,
            tool_name,
            normalize_arguments(arguments),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"idem_{digest}"


def idempotency_key_material(
    *,
    tenant_id: str,
    workflow_run_id: str,
    logical_step_id: str,
    tool_name: str,
    arguments: dict,
) -> str:
    """幂等键的「原料」，只用于日志/审计与排障展示，不参与存储键。"""
    return (
        f"{tenant_id}:{workflow_run_id}:{logical_step_id}:"
        f"{tool_name}:{normalize_arguments(arguments)}"
    )


def arguments_hash(arguments: dict) -> str:
    """§30 重复执行检测用的参数指纹（只关心参数本身，不含步骤）。"""
    return hashlib.sha256(normalize_arguments(arguments).encode("utf-8")).hexdigest()[:16]


# ======================================================================
# §5 Tool Metadata
# ======================================================================
class ToolMetadata(BaseModel):
    """Tool 自己声明的执行策略（§5）。Registry 是它的唯一来源（§59）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = "1.0"
    description: str = ""

    execution_mode: ExecutionMode = ExecutionMode.SYNC

    estimated_duration_ms: int = 100
    timeout_ms: int = 5000

    cpu_limit: float = 1.0
    memory_limit_mb: int = 512

    network_access: bool = False

    idempotent: bool = True

    risk_level: RiskLevel = RiskLevel.LOW

    required_permissions: list[str] = Field(default_factory=list)

    # ---- 说明书 §57 / §35 的扩展：不改变 §5 的形状，只做补充 ----
    idempotency_level: IdempotencyLevel = IdempotencyLevel.PURE
    """§57 比 ``idempotent: bool`` 更细：决定租约过期后能否直接接管重跑。"""

    fallback_tool: Optional[str] = None
    """§35 TOOL_NOT_FOUND / 循环降级时的替代 Tool。"""

    @property
    def is_long_running(self) -> bool:
        """超过 10s 归为长任务（§4.2）。"""
        return self.estimated_duration_ms > 10_000


# ======================================================================
# §6 Tool Call
# ======================================================================
class ToolCall(BaseModel):
    """Agent 提交的标准化调用请求（§6）。

    Agent **不应该**直接调用 Python function —— 一切经由此结构进入平台，
    平台才有机会做校验、权限、幂等、调度。
    """

    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(default_factory=new_call_id)
    agent_id: str = "agent_01"
    session_id: str = "session_01"
    graph_run_id: str = "run_001"

    tenant_id: str = "tenantA"

    tool_name: str
    tool_version: str = "1.0"

    arguments: dict[str, Any] = Field(default_factory=dict)

    idempotency_key: str = ""

    # §7 幂等键的第三个组成部分；默认取 graph_run_id 保持向后兼容
    logical_step_id: str = "step_1"

    parent_call_id: Optional[str] = None

    attempt: int = 0

    created_at: datetime = Field(default_factory=utcnow)

    # --- 便捷派生 ---
    def ensure_idempotency_key(self) -> str:
        """缺省时按 §7 配方补全（Agent 可以不自己算）。"""
        if not self.idempotency_key:
            self.idempotency_key = compute_idempotency_key(
                tenant_id=self.tenant_id,
                workflow_run_id=self.graph_run_id,
                logical_step_id=self.logical_step_id,
                tool_name=self.tool_name,
                arguments=self.arguments,
            )
        return self.idempotency_key

    @property
    def args_hash(self) -> str:
        return arguments_hash(self.arguments)

    def signature(self) -> str:
        """§30/§31 的「执行签名」：tool + 参数指纹，用于重复/循环检测。"""
        return f"{self.tool_name}:{self.args_hash}"

    def child(self, tool_name: str, arguments: dict[str, Any], *, step: str) -> "ToolCall":
        """派生一个子调用（parent_call_id 串起调用树，供 DAG 检测回溯）。"""
        return ToolCall(
            agent_id=self.agent_id,
            session_id=self.session_id,
            graph_run_id=self.graph_run_id,
            tenant_id=self.tenant_id,
            tool_name=tool_name,
            arguments=arguments,
            logical_step_id=step,
            parent_call_id=self.call_id,
        )


# ======================================================================
# §42 持久化记录（PostgreSQL = Durable Source of Truth）
# ======================================================================
class ExecutionRecord(BaseModel):
    """``tool_execution`` 表（§42）。"""

    model_config = ConfigDict(extra="allow")

    id: Optional[int] = None
    call_id: str
    run_id: str = ""
    agent_id: str = ""
    tenant_id: str = "tenantA"

    tool_name: str
    tool_version: str = "1.0"
    idempotency_key: str

    arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_hash: str = ""

    status: ExecutionStatus = ExecutionStatus.CREATED
    attempt: int = 0

    worker_id: Optional[str] = None
    lease_id: Optional[str] = None

    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    error_type: Optional[str] = None
    error_message: Optional[str] = None

    result_id: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ToolResultRecord(BaseModel):
    """``tool_result`` 表（§42）—— 小结果 inline，大结果只留 artifact 引用。"""

    model_config = ConfigDict(extra="allow")

    id: Optional[int] = None
    call_id: str

    result_type: Literal["inline", "artifact"] = "inline"
    inline_result: Optional[str] = None
    artifact_id: Optional[str] = None

    size_bytes: int = 0
    content_hash: str = ""

    created_at: datetime = Field(default_factory=utcnow)


class ExecutionEvent(BaseModel):
    """``execution_event`` 表（§42）—— audit / debug / recovery / observability。"""

    model_config = ConfigDict(extra="allow")

    id: Optional[int] = None
    call_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class OutboxRecord(BaseModel):
    """``outbox_event`` 表（§46）—— DB 事务提交成功之后才驱动 Redis 更新。

    保证不会出现「Redis 说 SUCCESS，PostgreSQL 还说 PROCESSING」的撕裂（§45）。
    """

    model_config = ConfigDict(extra="allow")

    id: Optional[int] = None
    aggregate_id: str            # 通常是 call_id
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    published: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    published_at: Optional[datetime] = None


# ======================================================================
# §8 / §14 协调层记录（Redis = 高性能协调层）
# ======================================================================
class IdempotencyRecord(BaseModel):
    """``idempotency:{key}`` 的 value（§8）。"""

    model_config = ConfigDict(extra="allow")

    status: IdempotencyStatus
    call_id: str = ""
    result_id: Optional[str] = None
    worker_id: Optional[str] = None
    lease_id: Optional[str] = None

    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    error: Optional[str] = None
    error_type: Optional[str] = None

    # 便于排障：幂等键原料
    key_material: Optional[str] = None

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str) -> "IdempotencyRecord":
        return cls.model_validate_json(raw)


class LeaseRecord(BaseModel):
    """``lease:{call_id}`` 的 value（§14）。

    ``expire_at`` 用 epoch 秒，便于 Lua 脚本里直接和 ``TIME`` 比较（§15）。
    """

    model_config = ConfigDict(extra="allow")

    worker_id: str
    lease_id: str
    call_id: str = ""
    expire_at: float = 0.0
    ttl_seconds: int = 30

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str) -> "LeaseRecord":
        return cls.model_validate_json(raw)


class PendingToolCall(BaseModel):
    """写在 LangGraph Checkpoint 里的「待完成 Tool 调用」（§18）。

    Agent 崩溃重启后，恢复流程只靠这一小段信息就能判断该等待、该取结果还是该重提交。
    """

    model_config = ConfigDict(extra="allow")

    call_id: str
    idempotency_key: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime = Field(default_factory=utcnow)
    attempt: int = 0


# ======================================================================
# §37-40 结果形态
# ======================================================================
class ArtifactRef(BaseModel):
    """大结果落 Object Storage 后返回给 Agent 的引用（§39）。"""

    artifact_id: str
    size: int = 0
    preview: str = ""
    content_type: str = "application/json"
    download_tool: str = "get_artifact"


class ResultEnvelope(BaseModel):
    """Result Processor 的产物 —— Agent 真正看到的东西。

    ``inline``：小结果，直接进 Context（§38）。
    ``artifact``：大结果，只给 preview + artifact_id（§39/§40）。
    """

    result_type: Literal["inline", "artifact"] = "inline"
    status: str = "success"

    data: Any = None                    # inline 时的载荷
    artifact: Optional[ArtifactRef] = None

    size_bytes: int = 0
    truncated: bool = False
    stats: dict[str, Any] = Field(default_factory=dict)

    def to_agent_view(self) -> dict[str, Any]:
        """压成 JSON-safe dict，直接塞进 LLM Context。"""
        if self.result_type == "inline":
            return {
                "status": self.status,
                "result_type": "inline",
                "data": self.data,
                "size_bytes": self.size_bytes,
            }
        assert self.artifact is not None
        return {
            "status": self.status,
            "result_type": "artifact",
            "artifact_id": self.artifact.artifact_id,
            "size": self.artifact.size,
            "preview": self.artifact.preview,
            "download_tool": self.artifact.download_tool,
            **({"stats": self.stats} if self.stats else {}),
        }


class SubmitResult(BaseModel):
    """``ToolGateway.submit()`` 的返回值 —— Agent / Tool Node 的唯一入口契约。"""

    call_id: str
    status: ExecutionStatus
    outcome: str  # SubmitOutcome.value，避免循环导入故存字符串

    execution_mode: ExecutionMode = ExecutionMode.SYNC
    result_id: Optional[str] = None
    result: Optional[ResultEnvelope] = None

    job_id: Optional[str] = None        # 异步任务句柄
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    attempt: int = 0
    deduplicated: bool = False
    duration_ms: int = 0

    # 恢复建议（§55）：Agent 据此决定 WAIT / 取结果 / 重提交
    resume_hint: Optional[RecoveryAction] = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_final(self) -> bool:
        """是否已经拿到终态结论（成功或确定失败）。"""
        return self.status in (ExecutionStatus.SUCCESS, ExecutionStatus.COMPLETED)


# ======================================================================
# §51-52 人工介入
# ======================================================================
class ApprovalRecord(BaseModel):
    """高风险 Tool 的审批单（§52）。

    ``agent_id`` / ``run_id`` 看起来像「元数据」，但它们**必须持久化**：

    审批通过之后要重新执行，而重新执行意味着**重建一个 ToolCall**。
    如果审批单里不记原始 ``agent_id``，重建时就只能拿个默认值 ——
    于是「管理员批准了操作员发起的高风险调用」会退化成
    「用错误身份去执行」，要么被权限层拒掉（批准了却跑不起来），
    要么更糟：用一个权限更大的默认身份跑起来，**绕过了原本的身份约束**。
    """

    model_config = ConfigDict(extra="allow")

    call_id: str
    idempotency_key: str = ""
    tool_name: str
    risk_level: RiskLevel = RiskLevel.HIGH
    arguments: dict[str, Any] = Field(default_factory=dict)

    # 重建 ToolCall 所必需的身份信息（见类 docstring）
    agent_id: str = ""
    session_id: str = ""
    run_id: str = ""
    tenant_id: str = "tenantA"
    logical_step_id: str = "step_1"

    status: Literal["WAITING_HUMAN", "APPROVED", "REJECTED", "MODIFIED"] = "WAITING_HUMAN"
    reason: str = ""
    reviewer: Optional[str] = None
    modified_arguments: Optional[dict[str, Any]] = None
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: Optional[datetime] = None

    def rebuild_call(self, *, arguments: dict[str, Any] | None = None) -> ToolCall:
        """按审批单重建原始 :class:`ToolCall`（§52 恢复执行）。

        ``idempotency_key`` 置空让平台重算：参数如果被 ``MODIFY`` 改过，
        旧的幂等键就不再描述这次操作了（§7 —— 参数变了，键必须变）。
        """
        return ToolCall(
            call_id=self.call_id,
            agent_id=self.agent_id or "agent_01",
            session_id=self.session_id or "session_01",
            graph_run_id=self.run_id or "run_001",
            tenant_id=self.tenant_id,
            tool_name=self.tool_name,
            arguments=dict(arguments if arguments is not None else self.arguments),
            idempotency_key="",
            logical_step_id=self.logical_step_id or "step_1",
        )


# ======================================================================
# §10 Redis 原子创建用的载荷
# ======================================================================
class ProcessingClaim(BaseModel):
    """``SET key value NX EX`` 成功时写入的「执行权占有」声明（§10）。"""

    status: IdempotencyStatus = IdempotencyStatus.PROCESSING
    call_id: str
    worker_id: Optional[str] = None
    lease_id: Optional[str] = None
    started_at: datetime = Field(default_factory=utcnow)
    key_material: Optional[str] = None
