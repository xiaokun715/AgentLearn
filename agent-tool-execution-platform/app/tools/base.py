"""Tool 基类与契约（说明书 §5 / §59 / §60）。

一个 Tool 由三部分组成：

1. **元数据** :class:`~app.domain.models.ToolMetadata` —— 声明执行策略（sync/async、
   timeout、资源、风险、权限、幂等性等级）。
2. **参数模型** ``args_model`` —— Pydantic 模型，即 §21 里 Tool 期待的 ``timeout: int``。
   Gateway 用它做首轮校验，也是参数自愈的**目标形状**。
3. **执行体** :meth:`BaseTool.run` —— 真正干活的那段代码。

Agent **永远不直接调** :meth:`BaseTool.run`。它只提交 :class:`~app.domain.models.ToolCall`，
由平台决定何时、在哪个沙箱、以什么权限执行（§1 的职责边界）。
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional, Type

from pydantic import BaseModel

from ..domain.models import ToolCall, ToolMetadata


@dataclass
class ToolOutput:
    """Tool 的**原始**产物。

    注意这里还不是 Agent 看到的东西 —— 它要经过 Result Processor（§37-40）
    做尺寸判断、截断与 artifact 化，才会变成 :class:`~app.domain.models.ResultEnvelope`。
    """

    data: Any = None
    content_type: str = "application/json"
    stats: dict = field(default_factory=dict)

    def size_bytes(self) -> int:
        """估算序列化体积 —— 决定走 inline 还是 artifact（§37）。"""
        import json

        if isinstance(self.data, (bytes, bytearray)):
            return len(self.data)
        try:
            return len(json.dumps(self.data, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            return len(str(self.data).encode("utf-8"))


@dataclass
class ToolContext:
    """执行一个 Tool 时能用到的一切外部能力。

    这是 Tool 与平台的**唯一接触面**：想跑命令就调 ``ctx.run_in_sandbox``，
    想写工作区就用 ``ctx.workspace``。Tool 拿不到数据库、拿不到 Redis ——
    它因此不可能绕过幂等与审计去产生副作用。
    """

    call: ToolCall
    metadata: ToolMetadata
    workspace: str = "."

    # 沙箱执行入口：由 scheduler 注入（绑定好 policy / runtime）
    run_in_sandbox: Optional[Any] = None

    clock: Any = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("tool"))

    # 允许 Tool 上报中间进度（长任务的进度事件，§50）
    progress: Optional[Any] = None

    started_at: float = field(default_factory=time.time)

    def elapsed_ms(self) -> int:
        return int((time.time() - self.started_at) * 1000)

    def emit_progress(self, percent: float, message: str = "") -> None:
        """上报进度；没有订阅者时静默丢弃。"""
        if self.progress is not None:
            self.progress(percent, message)

    def sandbox_run(
        self,
        argv: list[str],
        *,
        cwd: Optional[str] = None,
        stdin: str = "",
        timeout_seconds: Optional[float] = None,
    ):
        """在沙箱中执行命令；未注入沙箱时直接报错而不是偷偷裸跑。

        「未注入就拒绝执行」是刻意的：宁可失败，也不能让 Tool 在无隔离环境里跑起来。
        """
        if self.run_in_sandbox is None:
            raise RuntimeError(
                "ToolContext 未注入沙箱执行器，拒绝在无隔离环境下执行命令"
            )
        return self.run_in_sandbox(
            argv, cwd=cwd, stdin=stdin, timeout_seconds=timeout_seconds
        )


class BaseTool(ABC):
    """所有 Tool 的基类。

    子类只需声明三个类属性并实现 :meth:`run`::

        class CalculatorTool(BaseTool):
            name = "calculator"
            metadata = ToolMetadata(name="calculator", execution_mode="sync", ...)
            args_model = CalculatorArgs

            def run(self, args: CalculatorArgs, ctx: ToolContext) -> ToolOutput:
                return ToolOutput(data={"result": eval_expr(args.expression)})
    """

    name: ClassVar[str] = "unnamed"
    metadata: ClassVar[ToolMetadata]
    args_model: ClassVar[Type[BaseModel]]

    # 展示用：说明书 §60 推荐的分类（sync/async + 用途）
    category: ClassVar[str] = "general"

    @abstractmethod
    def run(self, args: BaseModel, ctx: ToolContext) -> ToolOutput:
        """执行 Tool。入参已通过 Schema 校验与参数自愈（§21-23）。"""

    # ------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """给 Agent / LLM 看的 Tool 描述（可转 tool-calling schema）。"""
        return {
            "name": self.metadata.name,
            "version": self.metadata.version,
            "description": self.metadata.description,
            "execution_mode": self.metadata.execution_mode.value,
            "risk_level": self.metadata.risk_level.value,
            "idempotency_level": self.metadata.idempotency_level.value,
            "timeout_ms": self.metadata.timeout_ms,
            "input_schema": self.args_model.model_json_schema(),
        }
