"""Execution 结果（设计说明书 §24）。

Sandbox 执行需要采集 stdout / stderr / exit_code，并返回给 Agent。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .execution import ExecutionStatus


@dataclass(slots=True)
class ExecutionResult:
    execution_id: str
    status: ExecutionStatus
    stdout: str
    stderr: str
    exit_code: int | None
    duration_ms: int
    error: str | None = None
    resource_usage: dict = field(default_factory=dict)
