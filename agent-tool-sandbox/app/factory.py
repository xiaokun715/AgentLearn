"""应用装配（工厂模式）—— 按配置组装所有组件。

存储：memory | sqlite（默认）| postgres
沙箱：auto（Docker 优先，退化 Process）| docker | process

与 async-agent-job-queue 的 factory.py 风格保持一致，方便复用。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import AppConfig
from .policy.compiler import PolicyCompiler
from .policy.engine import PolicyEngine
from .sandbox.manager import SandboxManager
from .security.audit import AuditLogger
from .service import ExecutionService
from .storage.execution_store import ExecutionStore
from .storage.policy_store import PolicyStore

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    """一次性持有全部组件，方便 API / 测试 / 脚本共用。"""

    config: AppConfig
    execution_store: ExecutionStore
    policy_store: PolicyStore
    manager: SandboxManager
    engine: PolicyEngine
    compiler: PolicyCompiler
    audit: AuditLogger
    service: ExecutionService
    _closers: list = field(default_factory=list)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        # 取消还在跑的后台执行，避免泄漏 asyncio 任务
        try:
            await self.service.cancel_all()
        except Exception:  # noqa: BLE001
            logger.exception("cancel_all failed")
        for closer in reversed(self._closers):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                logger.exception("close failed")


async def build_runtime(config: AppConfig | None = None) -> Runtime:
    """构造完整运行环境。PostgreSQL 后端未装依赖时抛清晰的错误。"""
    config = config or AppConfig.from_env()
    audit = AuditLogger(enabled=config.audit_enabled)
    closers: list = []

    # ---- 存储层 -------------------------------------------------------------
    if config.storage_backend == "memory":
        from .storage.memory import MemoryExecutionStore, MemoryPolicyStore

        execution_store: ExecutionStore = MemoryExecutionStore()
        policy_store: PolicyStore = MemoryPolicyStore()

    elif config.storage_backend == "postgres":
        try:
            from .storage.postgres import (
                PostgresDatabase,
                PostgresExecutionStore,
                PostgresPolicyStore,
            )
        except ImportError as exc:
            raise RuntimeError(
                "STORAGE_BACKEND=postgres 需要 pip install agent-tool-sandbox[postgres]"
            ) from exc
        db = PostgresDatabase(config.database_url)
        await db.connect()
        execution_store = PostgresExecutionStore(db)
        policy_store = PostgresPolicyStore(db)
        closers.append(db.close)

    else:  # sqlite（默认）
        from .storage.sqlite import (
            SqliteDatabase,
            SqliteExecutionStore,
            SqlitePolicyStore,
        )

        db = SqliteDatabase(config.database_url)
        await db.connect()
        execution_store = SqliteExecutionStore(db)
        policy_store = SqlitePolicyStore(db)
        closers.append(db.close)

    # ---- 业务层 -------------------------------------------------------------
    manager = SandboxManager(config)
    engine = PolicyEngine(policy_store)
    compiler = PolicyCompiler()
    service = ExecutionService(
        config=config,
        store=execution_store,
        policy_store=policy_store,
        engine=engine,
        compiler=compiler,
        manager=manager,
        audit=audit,
    )

    return Runtime(
        config=config,
        execution_store=execution_store,
        policy_store=policy_store,
        manager=manager,
        engine=engine,
        compiler=compiler,
        audit=audit,
        service=service,
        _closers=closers,
    )
