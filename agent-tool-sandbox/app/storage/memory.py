"""内存版存储后端 —— 零依赖、跑测试用，重启即丢。"""
from __future__ import annotations

import logging

from ..domain.execution import Execution
from ..domain.policy import Policy
from .execution_store import ExecutionStore
from .policy_store import PolicyStore, default_policy_objects

logger = logging.getLogger(__name__)


class MemoryExecutionStore(ExecutionStore):
    def __init__(self) -> None:
        self._items: dict[str, Execution] = {}

    async def save(self, execution: Execution) -> None:
        self._items[execution.id] = execution

    async def get(self, execution_id: str) -> Execution | None:
        return self._items.get(execution_id)

    async def list(self, *, limit: int = 50, tenant_id: str | None = None) -> list[Execution]:
        items = list(self._items.values())
        if tenant_id:
            items = [e for e in items if e.tenant_id == tenant_id]
        items.sort(key=lambda e: e.created_at, reverse=True)
        return items[:limit]


class MemoryPolicyStore(PolicyStore):
    def __init__(self) -> None:
        self._items: dict[str, Policy] = {}
        for policy in default_policy_objects():
            self._items[policy.name] = policy

    async def save(self, policy: Policy) -> None:
        self._items[policy.name] = policy

    async def get(self, name: str) -> Policy | None:
        return self._items.get(name)

    async def get_default(self) -> Policy | None:
        # 默认策略固定为第一个内置策略（python_basic）
        return self._items.get("python_basic")

    async def list(self) -> list[Policy]:
        return list(self._items.values())
