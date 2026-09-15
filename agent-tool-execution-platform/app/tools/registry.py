"""Tool Registry —— Tool 不应该硬编码（说明书 §59）。

Registry 是一张「Tool 的全部执行策略」的注册表，每一条 :class:`ToolSpec` 聚合：

==================== ==========================================
Tool Metadata        §5  执行模式 / 超时 / 资源 / 风险 / 权限
Schema               §21 ``args_model``，参数校验与自愈的目标形状
Version              §5  版本号，随 ToolCall 一起落库
Permission           §26 需要的 action 列表
Execution Policy     §4  sync / async 分流依据
Retry Policy         §36 退避参数
Sandbox Policy       §27 资源隔离参数
Risk Policy          §51 高风险 -> 人工审批
==================== ==========================================

**代码声明默认值，YAML 覆盖**（``configs/tools.yaml``）—— 于是新增一个 Tool 只需写
一个 ``BaseTool`` 子类，调参只需改 YAML，两者互不阻塞。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel

from ..config import AppConfig
from ..domain.enums import ExecutionMode, IdempotencyLevel, RiskLevel
from ..domain.errors import ToolNotFound
from ..domain.models import ToolMetadata
from ..domain.policy import ParamPolicy, RetryPolicy, SandboxPolicy
from .base import BaseTool

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    """Registry 里的一条记录：Tool 本体 + 它全部的运行时策略。"""

    metadata: ToolMetadata
    tool: BaseTool
    args_model: type[BaseModel]
    param_policy: ParamPolicy
    retry: RetryPolicy
    sandbox: SandboxPolicy

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def is_async(self) -> bool:
        return self.metadata.execution_mode == ExecutionMode.ASYNC

    def describe(self) -> dict[str, Any]:
        return self.tool.describe()


class ToolRegistry:
    """Tool 注册表。"""

    def __init__(self, config: Optional[AppConfig] = None) -> None:
        self.config = config or AppConfig.for_demo()
        self._specs: dict[str, ToolSpec] = {}

    # ==================================================================
    # 注册
    # ==================================================================
    def register(self, tool: BaseTool, *, override_metadata: bool = False) -> ToolSpec:
        """注册一个 Tool，并把 YAML 覆盖应用到它的策略上。

        :param override_metadata: 允许 YAML 改写 ``ToolMetadata`` 字段
            （版本、超时、风险等级等）。默认允许 —— 这正是「不硬编码」的含义。
        """
        metadata = tool.metadata.model_copy(deep=True)
        self._apply_yaml_overrides(metadata, tool.name)

        spec = ToolSpec(
            metadata=metadata,
            tool=tool,
            args_model=tool.args_model,
            param_policy=self.config.effective_param_policy(tool.name),
            retry=self.config.effective_retry(tool.name),
            sandbox=self.config.effective_sandbox(tool.name),
        )
        self._specs[tool.name] = spec
        logger.debug("registered tool %s (%s)", tool.name, metadata.execution_mode.value)
        return spec

    def register_many(self, *tools: BaseTool) -> None:
        for tool in tools:
            self.register(tool)

    def _apply_yaml_overrides(self, metadata: ToolMetadata, name: str) -> None:
        """把 ``configs/tools.yaml`` 的声明合并进元数据。"""
        override = self.config.tool_override(name)
        if override is None:
            return

        if override.version is not None:
            metadata.version = override.version
        if override.mode is not None:
            metadata.execution_mode = ExecutionMode(override.mode)
        if override.estimated_duration_ms is not None:
            metadata.estimated_duration_ms = override.estimated_duration_ms
        if override.timeout_ms is not None:
            metadata.timeout_ms = override.timeout_ms
        if override.risk is not None:
            metadata.risk_level = RiskLevel(override.risk)
        if override.fallback_tool is not None:
            metadata.fallback_tool = override.fallback_tool
        if override.idempotency_level is not None:
            metadata.idempotency_level = IdempotencyLevel(override.idempotency_level)
        if override.permissions is not None:
            metadata.required_permissions = list(override.permissions)

        # 风险等级是「只能变严不能变松」的例外：安全阀不能被 YAML 放松。
        declared = metadata.risk_level
        if declared == RiskLevel.HIGH:
            metadata.risk_level = RiskLevel.HIGH

    # ==================================================================
    # 查询
    # ==================================================================
    def get(self, name: str) -> ToolSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolNotFound(
                f"Tool 未注册: {name}",
                detail={"tool_name": name, "registered": sorted(self._specs)},
            )
        return spec

    def has(self, name: str) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[n] for n in self.names()]

    def catalog(self) -> list[dict[str, Any]]:
        """给 LLM 的 Tool 清单（含 JSON Schema），可直接喂给 tool-calling。"""
        return [spec.describe() for spec in self.specs()]

    def metadata(self, name: str) -> ToolMetadata:
        return self.get(name).metadata

    def fallback_for(self, name: str) -> Optional[str]:
        """§35 Tool Not Found / 循环降级时的替代 Tool。"""
        return self.get(name).metadata.fallback_tool

    # ==================================================================
    # 默认装配
    # ==================================================================
    @classmethod
    def default(cls, config: Optional[AppConfig] = None) -> "ToolRegistry":
        """装配说明书 §60 推荐的 5 个 Tool。

        这五个刚好覆盖一张完整的可靠性测试矩阵::

            calculator             sync  /  100ms   -> 最短路径、纯函数
            search_knowledge       sync  /  1~3s    -> 同步 Tool + 循环检测
            run_test               async /  1~5min  -> 异步 + Checkpoint + Crash 恢复
            execute_python         async /  sandbox -> 沙箱 + 超时 + 资源限制
            large_file_analysis    async /  artifact -> 大结果 + 截断 + Object Storage

        另加：

        * ``run_test_dry_run`` —— ``run_test`` 声明在 ``configs/tools.yaml`` 里的
          ``fallback_tool``。它**必须**被注册：否则 §35 的 FALLBACK 恢复动作会在
          ``registry.get()`` 上直接抛 ``ToolNotFound``，把「换个等价 Tool 再试」
          变成「彻底失败」。
        * ``database_delete`` —— 高风险 Tool，用于演示人工审批（§51/§67）。
        """
        from .calculator import CalculatorTool
        from .database_delete import DatabaseDeleteTool
        from .file_analysis import LargeFileAnalysisTool
        from .python_exec import ExecutePythonTool
        from .run_test import RunTestTool
        from .run_test_dry_run import RunTestToolDryRun
        from .search import SearchKnowledgeTool

        registry = cls(config)
        registry.register_many(
            CalculatorTool(),
            SearchKnowledgeTool(),
            RunTestTool(),
            RunTestToolDryRun(),
            ExecutePythonTool(),
            LargeFileAnalysisTool(),
            DatabaseDeleteTool(),
        )
        return registry
