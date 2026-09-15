"""依赖装配 —— 把各子系统拼成一个可运行的平台（说明书 §2 / §54）。

集中在一个地方 new 出所有组件，好处是**依赖方向显式可见**：
从上往下读 :meth:`build`，就是一张「谁依赖谁」的图。
散落在各处做 ``import`` 加单例会让循环依赖和测试替换都变得非常难受。

装配顺序刻意与数据的流动方向一致：

::

    Clock / Redis / DB / ObjectStore
        -> Registry
        -> Sandbox Manager
        -> Lease / Idempotency / Result / Recovery / Loop
        -> Executor -> Scheduler -> Worker -> Gateway
        -> AgentRuntime

最后一步 :class:`Platform` 提供 ``start_workers()`` / ``drain()`` 这样的运维动作，
让 ``main.py`` 不必自己管理线程生命周期。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .agent.checkpoint import CheckpointConfig, CheckpointManager
from .agent.graph import AgentRuntime
from .agent.recovery import AgentRecoveryManager
from .config import AppConfig
from .domain.enums import RiskLevel
from .gateway.gateway import ToolGateway
from .gateway.injection import InjectionDetector
from .gateway.permission import PermissionManager
from .gateway.validation import ArgumentValidator, ParameterRepairer
from .human.approval import HumanApprovalManager
from .idempotency.lock import DistributedLock
from .idempotency.manager import IdempotencyManager
from .infra.clock import Clock, SystemClock
from .infra.database import Database
from .infra.object_store import InMemoryObjectStore, LocalObjectStore, ObjectStore
from .infra.redis import RedisSim
from .lease.heartbeat import AgentHeartbeat, WorkerHeartbeat
from .lease.manager import LeaseManager
from .lease.reaper import LeaseReaper
from .loop.cycle import CycleDetector
from .loop.duplicate import DuplicateDetector
from .observability.audit import AuditLog
from .observability.metrics import Metrics
from .observability.tracing import Tracer
from .recovery.classifier import ErrorClassifier
from .recovery.policy import RecoveryPolicyEngine
from .recovery.retry import RetryController
from .result.artifact import ArtifactStore
from .result.store import ResultStore
from .sandbox.manager import SandboxManager
from .scheduler.async_executor import AsyncExecutor
from .scheduler.scheduler import Scheduler
from .scheduler.sync_executor import Executor, SyncExecutor
from .scheduler.worker import Worker
from .tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class _BoundHeartbeat:
    """把 :class:`WorkerHeartbeat` 绑到某个具体 call 上。

    Worker 拿到的工厂签名是 ``factory(call_id, lease_id=..., worker_id=...) -> 有 start/stop 的对象``，
    而 :class:`WorkerHeartbeat` 的方法是带参数的。这层薄适配让两边都不必迁就对方。
    """

    def __init__(self, heartbeat: WorkerHeartbeat, call_id: str, lease_id: str, worker_id: str) -> None:
        self._hb = heartbeat
        self._call_id = call_id
        self._lease_id = lease_id
        self._worker_id = worker_id

    def start(self) -> None:
        self._hb.start(self._call_id, lease_id=self._lease_id, worker_id=self._worker_id)

    def stop(self) -> None:
        self._hb.stop(self._call_id)


@dataclass
class Platform:
    """装配完成的平台。所有子系统都在这里可访问，便于演示与排障。"""

    config: AppConfig
    clock: Clock
    db: Database
    redis: RedisSim
    object_store: ObjectStore

    registry: ToolRegistry
    sandbox: SandboxManager

    artifacts: ArtifactStore
    results: ResultStore
    leases: LeaseManager
    idempotency: IdempotencyManager
    locks: DistributedLock

    classifier: ErrorClassifier
    retry: RetryController
    recovery: RecoveryPolicyEngine
    duplicates: DuplicateDetector
    cycles: CycleDetector
    approvals: HumanApprovalManager
    reaper: LeaseReaper

    metrics: Metrics
    tracer: Tracer
    audit: AuditLog

    validator: ArgumentValidator
    injection: InjectionDetector
    permission: PermissionManager

    executor: Executor
    sync_executor: SyncExecutor
    async_executor: Optional[AsyncExecutor]
    scheduler: Scheduler
    gateway: ToolGateway

    agent_heartbeat: AgentHeartbeat
    checkpoints: Optional[CheckpointManager] = None

    # 运行期资源
    workers: list[Worker] = field(default_factory=list)
    agent: Optional[AgentRuntime] = None
    agent_recovery: Optional[AgentRecoveryManager] = None

    # ==================================================================
    # 运维动作
    # ==================================================================
    def start_workers(self, count: Optional[int] = None) -> list[Worker]:
        self.workers = self.scheduler.start_workers(count)
        return self.workers

    def stop_workers(self) -> None:
        self.scheduler.stop_workers()
        self.workers = []

    def drain(self, *, max_iterations: int = 5000) -> int:
        """把队列里的异步任务（含到期重试）跑完。"""
        return self.scheduler.drain(max_iterations=max_iterations)

    def reap_once(self) -> list[Any]:
        """跑一轮租约回收（§56），返回接管结论。"""
        return self.reaper.scan_once()

    def publish_outbox(self, *, limit: int = 100) -> int:
        """补投 Outbox 事件（§46），保持 Redis 与 DB 的最终一致。"""
        return self.gateway.publish_outbox(limit=limit)

    def build_agent(self, *, checkpoint_db: str = "checkpoints.sqlite") -> AgentRuntime:
        """构造 LangGraph Agent 运行时（§47~§50）。"""
        self.checkpoints = CheckpointManager(CheckpointConfig(db_path=checkpoint_db))
        self.agent = AgentRuntime(
            self.gateway,
            self.registry,
            config=self.config,
            checkpoint=self.checkpoints,
            approvals=self.approvals,
        )
        self.agent_recovery = AgentRecoveryManager(
            self.gateway, self.db, self.idempotency, self.leases, self.config
        )
        return self.agent

    def close(self) -> None:
        """释放资源。幂等。"""
        self.stop_workers()
        if self.checkpoints is not None:
            try:
                self.checkpoints.close()
            except Exception:  # noqa: BLE001
                logger.debug("checkpoint close failed", exc_info=True)
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            logger.debug("db close failed", exc_info=True)

    def summary(self) -> dict[str, Any]:
        """平台全景快照 —— 演示收尾与排障都靠它。"""
        return {
            "tools": self.registry.names(),
            "gateway": self.gateway.describe(),
            "executions": self.db.count_by_status(),
            "queue": self.scheduler.queue_stats(),
            "workers": self.scheduler.worker_stats(),
            "metrics": self.metrics.snapshot() if self.metrics.enabled else {},
        }


def check_config(config: AppConfig, registry: ToolRegistry) -> list[str]:
    """装配前的配置自检。返回问题清单（空 = 全部合规）。

    放在 ``build_platform`` 里自动执行，是因为这些问题**只在故障时才显形**：

    * §29 的三层超时顺序错了 —— 平时看不出来，只有 Tool 跑超时那一刻才会
      发现「沙箱抢先把进程杀了」，于是错误类型从 TIMEOUT 退化成 SANDBOX_ERROR，
      Recovery Policy 表里 TIMEOUT 那一行的处置（Retry / Increase timeout）根本不会被执行。
    * 声明了 ``fallback_tool`` 却没注册 —— 只有真的需要 Fallback 那天才会
      ``ToolNotFound``，而那正是系统已经在故障中的时候。

    这类「配置层面的静默炸弹」值得在启动时用几十毫秒换掉。
    """
    from .sandbox.policy import validate_timeout_layers

    problems: list[str] = []
    for spec in registry.specs():
        meta = spec.metadata

        # 用**有效**租约 TTL 而不是配置里的裸值：Executor 会按
        # max(lease_ttl, tool_timeout + 30) 自动抬升，自检必须跟着算，
        # 否则每条异步 Tool 都会被误报成「租约太短」。
        effective_lease_ttl = int(max(config.lease_ttl_seconds, meta.timeout_ms / 1000.0 + 30))

        problems.extend(
            f"[{spec.name}] {msg}"
            for msg in validate_timeout_layers(
                tool_timeout_ms=meta.timeout_ms,
                sandbox_timeout_s=spec.sandbox.timeout_seconds,
                lease_ttl_s=effective_lease_ttl,
            )
        )

        if meta.fallback_tool and not registry.has(meta.fallback_tool):
            problems.append(
                f"[{spec.name}] 声明了 fallback_tool='{meta.fallback_tool}' 但它没有注册；"
                "一旦触发 §35 的 FALLBACK 恢复动作会直接 ToolNotFound"
            )

        if (
            meta.execution_mode.value == "async"
            and not meta.idempotent
            and meta.risk_level != RiskLevel.HIGH
        ):
            # HIGH 风险不算问题：那类 Tool 本来就要过人工审批（§51），
            # 「Worker 崩溃后无法判断」的场景会落在人手里，而不是自动接管。
            problems.append(
                f"[{spec.name}] 异步 + 非幂等 + 非高风险是危险组合：任务入队后 Agent 就放手了，"
                "Worker 崩溃时既无法判断副作用是否已经发生，也没有人工闸门兜底（§56/§57）"
            )
    return problems


def build_platform(
    config: Optional[AppConfig] = None,
    *,
    clock: Optional[Clock] = None,
    object_store: Optional[ObjectStore] = None,
    on_progress: Any = None,
    on_result: Any = None,
    enable_workers: bool = True,
    heartbeat_interval: Optional[float] = None,
) -> Platform:
    """按依赖顺序装配整个平台。

    :param enable_workers: 是否自动拉起后台 Worker 线程。
        演示里常常希望「手动推一格看一步」，那就传 ``False``，
        再用 ``platform.scheduler.drain()`` 或 ``worker.run_once()`` 自己控制节奏。
    """
    cfg = config or AppConfig.for_demo()
    clk = clock or SystemClock()

    # ---------------- 基础设施 ----------------
    redis = RedisSim(clk)
    db = Database(cfg.db_path)
    store: ObjectStore = object_store or (
        LocalObjectStore(cfg.artifact_root) if cfg.db_path != ":memory:" else InMemoryObjectStore()
    )

    audit = AuditLog(db)
    metrics = Metrics(enabled=cfg.metrics_enabled)
    tracer = Tracer(enabled=cfg.trace_enabled, clock=clk)
    classifier = ErrorClassifier()
    retry = RetryController()

    # ---------------- Tool 层 ----------------
    registry = ToolRegistry.default(cfg)
    for problem in check_config(cfg, registry):
        logger.warning("配置自检: %s", problem)
    sandbox = SandboxManager(cfg, clock=clk, backend=cfg.sandbox_backend)

    # ---------------- 结果 ----------------
    artifacts = ArtifactStore(store, clock=clk)
    results = ResultStore(db, artifacts, cfg)

    # ---------------- 协调层 ----------------
    leases = LeaseManager(redis, cfg, clock=clk)
    locks = DistributedLock(redis, ttl_seconds=cfg.lock_ttl_seconds, clock=clk)
    idempotency = IdempotencyManager(redis, db, cfg, clock=clk)

    # ---------------- 安全 ----------------
    validator = ArgumentValidator(registry, cfg, repairer=ParameterRepairer())
    injection = InjectionDetector(enabled=True)
    permission = PermissionManager(cfg)

    # ---------------- 恢复 / 检测 ----------------
    recovery = RecoveryPolicyEngine(cfg, registry, retry=retry)
    duplicates = DuplicateDetector(cfg, clock=clk)
    cycles = CycleDetector(cfg, clock=clk)
    approvals = HumanApprovalManager(redis, db, cfg, clock=clk)

    # ---------------- 执行 ----------------
    executor = Executor(
        registry=registry,
        config=cfg,
        sandbox_manager=sandbox,
        result_store=results,
        lease_manager=leases,
        audit=audit,
        metrics=metrics,
        tracer=tracer,
        clock=clk,
        classifier=classifier,
        on_progress=on_progress,
    )
    sync_executor = SyncExecutor(
        executor,
        recovery=recovery,
        retry=retry,
        registry=registry,
        config=cfg,
        audit=audit,
        metrics=metrics,
        clock=clk,
    )
    async_executor = AsyncExecutor(redis, cfg, clock=clk)

    # Worker 的心跳：TTL 与续租间隔由配置决定（§15 的 30s / 10s 关系）
    worker_heartbeat = WorkerHeartbeat(
        leases,
        interval_seconds=heartbeat_interval or cfg.heartbeat_interval_seconds,
        ttl_seconds=cfg.lease_ttl_seconds,
    )

    def _heartbeat_factory(call_id: str, *, lease_id: str, worker_id: str) -> _BoundHeartbeat:
        return _BoundHeartbeat(worker_heartbeat, call_id, lease_id, worker_id)

    def _worker_factory(name: str) -> Worker:
        return Worker(
            name=name,
            redis=redis,
            config=cfg,
            registry=registry,
            executor=executor,
            recovery=recovery,
            retry=retry,
            idempotency=idempotency,
            async_executor=async_executor,
            lease_manager=leases,
            heartbeat_factory=_heartbeat_factory,
            audit=audit,
            metrics=metrics,
            approvals=approvals,
            clock=clk,
            on_result=on_result,
        )

    scheduler = Scheduler(
        registry=registry,
        config=cfg,
        sync_executor=sync_executor,
        async_executor=async_executor,
        sandbox_manager=sandbox,
        redis=redis,
        audit=audit,
        metrics=metrics,
        worker_factory=_worker_factory,
        clock=clk,
    )

    # Executor 的取消判断要能看到 Scheduler 的状态，用闭包延迟绑定打破先有鸡先有蛋
    executor.cancel_check = scheduler.is_cancelled

    # ---------------- 入口 ----------------
    gateway = ToolGateway(
        registry=registry,
        config=cfg,
        scheduler=scheduler,
        db=db,
        redis=redis,
        validator=validator,
        injection=injection,
        permission=permission,
        idempotency=idempotency,
        lease_manager=leases,
        result_store=results,
        recovery=recovery,
        duplicates=duplicates,
        cycles=cycles,
        approvals=approvals,
        metrics=metrics,
        tracer=tracer,
        audit=audit,
        classifier=classifier,
        clock=clk,
    )

    # ---------------- §56 租约回收 ----------------
    reaper = LeaseReaper(
        redis,
        db,
        cfg,
        registry,
        clock=clk,
        lease_manager=leases,
        idempotency=idempotency,
        on_recover=lambda call_id: _requeue(db, async_executor, registry, call_id),
    )

    agent_heartbeat = AgentHeartbeat(redis, cfg, clock=clk)

    platform = Platform(
        config=cfg,
        clock=clk,
        db=db,
        redis=redis,
        object_store=store,
        registry=registry,
        sandbox=sandbox,
        artifacts=artifacts,
        results=results,
        leases=leases,
        idempotency=idempotency,
        locks=locks,
        classifier=classifier,
        retry=retry,
        recovery=recovery,
        duplicates=duplicates,
        cycles=cycles,
        approvals=approvals,
        reaper=reaper,
        metrics=metrics,
        tracer=tracer,
        audit=audit,
        validator=validator,
        injection=injection,
        permission=permission,
        executor=executor,
        sync_executor=sync_executor,
        async_executor=async_executor,
        scheduler=scheduler,
        gateway=gateway,
        agent_heartbeat=agent_heartbeat,
    )

    # Tool 完成后唤醒等待中的 Agent（§50 Tool Callback）
    gateway.on_completion(platform_placeholder_hook(db, registry))

    if enable_workers:
        platform.start_workers()

    return platform


def platform_placeholder_hook(db: Database, registry: ToolRegistry):
    """§50 的默认完成回调：只往审计流里放一条事件。

    Agent 的 Graph 恢复由 :class:`~app.agent.recovery.AgentRecoveryManager` 主动拉取，
    这里不反向调用 Graph —— 平台不该知道 Graph 的存在（§2 的职责边界）。
    """

    def _hook(call_id: str, result: Any) -> None:
        db.append_event(
            call_id,
            "tool.callback",
            {"event": "tool.completed", "call_id": call_id, "status": result.status.value},
        )

    return _hook


def _requeue(db: Database, async_executor: Optional[AsyncExecutor], registry: ToolRegistry, call_id: str) -> bool:
    """§56 的 ``on_recover``：把被接管的任务重新放回队列。

    从 DB 读回原始参数重新入队，而不是从内存里找 —— 因为接管者**不是**原来的进程，
    它手里没有原 Worker 的任何内存状态。这也顺带证明了 DB 作为
    Source of Truth 的必要性（§44）。
    """
    record = db.get_execution(call_id)
    if record is None or async_executor is None:
        return False
    if not registry.has(record.tool_name):
        return False

    from .domain.models import ToolCall
    from .scheduler.async_executor import JobPayload

    spec = registry.get(record.tool_name)
    call = ToolCall(
        call_id=record.call_id,
        agent_id=record.agent_id,
        graph_run_id=record.run_id,
        tenant_id=record.tenant_id,
        tool_name=record.tool_name,
        tool_version=record.tool_version,
        arguments=dict(record.arguments),
        idempotency_key=record.idempotency_key,
        attempt=record.attempt + 1,
    )
    payload = JobPayload.from_call(
        call, risk_level=spec.metadata.risk_level.value, timeout_ms=spec.metadata.timeout_ms
    )
    async_executor.retry_later(payload, delay_ms=0)
    db.append_event(call_id, "lease.recovered", {"requeued": True, "attempt": call.attempt})
    return True
