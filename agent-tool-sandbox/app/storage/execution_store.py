"""ExecutionStore 接口（设计说明书 §34 executions 表）。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..domain.execution import Execution


class ExecutionStore(ABC):
    """Execution 的读写接口。memory / sqlite / postgres 三种实现。"""

    @abstractmethod
    async def save(self, execution: Execution) -> None:
        """插入或更新一次执行。"""

    @abstractmethod
    async def get(self, execution_id: str) -> Execution | None:
        """按 id 读取。"""

    @abstractmethod
    async def list(self, *, limit: int = 50, tenant_id: str | None = None) -> list[Execution]:
        """按创建时间倒序列出（可选按 tenant 过滤）。"""
