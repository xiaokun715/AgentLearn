"""Execution Service —— 串联 Policy → Compiler → Sandbox → Executor → Audit（设计说明书 §38）。

完整的 Agent Loop 里，Tool Call 会走到这里：
    Tool Call → Policy Engine → (DENY → REJECTED) / (ALLOW → Sandbox → Result)

这里不决定权限，只负责编排；权限由 Policy Engine 决定。
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from .config import AppConfig
from .domain.execution import Execution, ExecutionStatus
from .domain.exceptions import ExecutionNotFound, PolicyNotFound
from .domain.policy import Policy
from .domain.result import ExecutionResult
from .policy.compiler import PolicyCompiler
from .policy.engine import PolicyDecision, PolicyEngine
from .runtime.executor import ExecutionExecutor
from .runtime.resource import ResourceMonitor
from .sandbox.manager import SandboxManager
from .security.audit import AuditLogger
from .security.identity import Identity
from .storage.execution_store import ExecutionStore
from .storage.policy_store import PolicyStore

logger = logging.getLogger(__name__)


class ExecutionService:
    """对外唯一入口：创建 / 查询 / kill 一次沙箱执行。"""

    def __init__(
        self,
        config: AppConfig,
        store: ExecutionStore,
        policy_store: PolicyStore,
        engine: PolicyEngine,
        compiler: PolicyCompiler,
        manager: SandboxManager,
        audit: AuditLogger,
    ) -> None:
        self.config = config
        self.store = store
        self.policy_store = policy_store
        self.engine = engine
        self.compiler = compiler
        self.manager = manager
        self.audit = audit
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ create
    async def create(
        self,
        *,
        tool_type: str,
        code: str,
        policy_request: dict | None,
        identity: Identity,
    ) -> Execution:
        """创建一次执行，返回 QUEUED 状态；后台任务负责 POLICY_CHECK → RUNNING → 终态。"""
        policy_name = (policy_request or {}).get("name") or self.config.default_policy

        execution = Execution(
            id=f"exec_{uuid.uuid4().hex[:12]}",
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            agent_id=identity.agent_id,
            tool_type=tool_type,
            code=code,
            status=ExecutionStatus.QUEUED,
            policy_id=policy_name,
        )
        await self.store.save(execution)
        self.manager.register(execution.id)

        self.audit.emit(
            execution_id=execution.id, event_type="execution.created", identity=identity,
            payload={"tool": tool_type, "policy": policy_name},
        )

        task = asyncio.create_task(self._process(execution, policy_request or {}, identity))
        self._tasks[execution.id] = task
        return execution

    # ----------------------------------------------------------------- process
    async def _process(self, execution: Execution, policy_request: dict, identity: Identity) -> None:
        """后台执行主流程（状态机 §8）。在并发配额内运行。"""
        try:
            await self.manager.gate(self._run(execution, policy_request, identity))
        except asyncio.CancelledError:
            logger.info("execution task cancelled: %s", execution.id)
            raise
        except Exception as exc:  # noqa: BLE001 —— 意外异常也要落终态
            logger.exception("unexpected error processing execution %s", execution.id)
            execution.status = ExecutionStatus.FAILED
            execution.error = str(exc)
            execution.mark_finished(ExecutionStatus.FAILED)
            await self.store.save(execution)
            self.audit.emit(
                execution_id=execution.id, event_type="execution.failed", identity=identity,
                payload={"error": str(exc)},
            )
        finally:
            self._tasks.pop(execution.id, None)
            self.manager.untrack(execution.id)

    async def _run(self, execution: Execution, policy_request: dict, identity: Identity) -> None:
        # 排队期间已被 kill → 不再启动（幂等）
        if execution.kill_requested:
            execution.status = ExecutionStatus.KILLED
            execution.mark_finished(ExecutionStatus.KILLED)
            await self.store.save(execution)
            return

        # ---------- POLICY_CHECK（§8 状态机；DENY → REJECTED） ----------
        execution.status = ExecutionStatus.POLICY_CHECK
        await self.store.save(execution)

        try:
            decision: PolicyDecision = await self.engine.evaluate(
                tool_type=execution.tool_type,
                code=execution.code,
                policy_name=policy_request.get("name"),
                requested=policy_request,
            )
        except PolicyNotFound as exc:
            execution.status = ExecutionStatus.REJECTED
            execution.error = str(exc)
            execution.mark_finished(ExecutionStatus.REJECTED)
            await self.store.save(execution)
            self.audit.emit(
                execution_id=execution.id, event_type="execution.rejected", identity=identity,
                payload={"reason": str(exc)},
            )
            return

        if not decision.allowed:
            execution.status = ExecutionStatus.REJECTED
            execution.error = decision.reason
            execution.mark_finished(ExecutionStatus.REJECTED)
            await self.store.save(execution)
            self.audit.emit(
                execution_id=execution.id, event_type="execution.rejected", identity=identity,
                payload={"reason": decision.reason, "warnings": decision.warnings},
            )
            logger.warning("execution %s REJECTED: %s", execution.id, decision.reason)
            return

        # kill 已在排队期间被请求 → 不再启动 runtime
        if execution.kill_requested:
            execution.status = ExecutionStatus.KILLED
            execution.mark_finished(ExecutionStatus.KILLED)
            await self.store.save(execution)
            return

        # ---------- ALLOW → Sandbox（§38） ----------
        config = self.compiler.compile(decision.policy, execution.tool_type)
        executor = ExecutionExecutor(
            self.manager.get_sandbox(),
            ResourceMonitor(interval=self.config.monitor_interval),
        )
        kill_event = self.manager.register(execution.id)

        result: ExecutionResult = await executor.run(
            execution, config, kill_event,
            track_runtime=lambda eid, rid: self.manager.track(eid, rid),
        )

        # ---------- 落结果 ----------
        execution.status = result.status
        execution.exit_code = result.exit_code
        execution.stdout = result.stdout
        execution.stderr = result.stderr
        execution.error = result.error
        execution.resource_usage = result.resource_usage
        execution.mark_finished(result.status)
        await self.store.save(execution)

        self.audit.emit(
            execution_id=execution.id, event_type="execution.finished", identity=identity,
            payload=execution.to_audit_dict(),
        )
        logger.info("execution %s -> %s (%.0fms)", execution.id, result.status.value,
                    result.duration_ms)

    # ------------------------------------------------------------------- get
    async def get(self, execution_id: str) -> Execution:
        execution = await self.store.get(execution_id)
        if execution is None:
            raise ExecutionNotFound(f"execution not found: {execution_id}")
        return execution

    async def list(self, *, limit: int = 50, tenant_id: str | None = None) -> list[Execution]:
        return await self.store.list(limit=limit, tenant_id=tenant_id)

    # ------------------------------------------------------------------- kill
    async def kill(self, execution_id: str) -> Execution:
        """Kill Switch（§22）：幂等（§23）。重复 kill 返回同一 killed 状态，不报错。"""
        execution = await self.get(execution_id)

        if execution.status.terminal:
            return execution  # 已结束：幂等返回，不报 500

        execution.kill_requested = True
        await self.store.save(execution)
        await self.manager.kill(execution_id)

        self.audit.emit(
            execution_id=execution_id, event_type="execution.kill_requested",
            payload={"status_before": execution.status.value},
        )

        # 立即落终态（§23 返回 status: killed）。后台 executor 会收敛到同一状态。
        # runtime 已建 → 已被 manager.kill 真实终止；未建 → 只是取消启动。
        if not execution.status.terminal:
            execution.status = ExecutionStatus.KILLED
            execution.mark_finished(ExecutionStatus.KILLED)
            await self.store.save(execution)
        return execution

    # ------------------------------------------------------------------- wait
    async def wait(self, execution_id: str, timeout: float = 30.0) -> Execution:
        """等待后台任务完成（测试 / 同步客户端用）。"""
        task = self._tasks.get(execution_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return await self.get(execution_id)

    # ------------------------------------------------------------ cancellation
    async def cancel_all(self) -> None:
        """取消所有后台任务（服务关闭 / 测试 teardown 时调用）。"""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- policies
    async def list_policies(self) -> list[Policy]:
        return await self.policy_store.list()
