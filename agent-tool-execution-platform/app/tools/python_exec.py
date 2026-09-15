"""ExecutePythonTool —— §60 的**沙箱执行**主力（说明书 §27 / §28 / §29 / §60）。

§60 矩阵第四行：

    ====================  ========  ==========  ==================================
    Tool                  模式      耗时        被用来验证什么
    ====================  ========  ==========  ==================================
    execute_python        async     沙箱        沙箱 + 超时 + 资源限制
    ====================  ========  ==========  ==================================

**它是整个平台唯一一个「真的执行任意代码」的 Tool**，因此也是所有安全设计的
试金石：

- **§27 隔离**：代码**只能**通过 ``ctx.sandbox_run`` 执行，绝不 ``exec()``
  在当前进程里跑。Tool 与平台共享同一个解释器这个前提一旦被打破
  （用户代码 ``import os; os._exit(0)`` 或者改掉平台的全局状态），
  后面所有可靠性机制都没有意义了。未注入沙箱时 ``sandbox_run`` 会直接报错，
  这是刻意的「宁可失败也不裸跑」。
- **§28 流程**：Workspace 由平台准备（``ctx.workspace`` 作为 ``cwd``），
  Tool 只负责在里面跑，收集输出后由执行器销毁。
- **§29 超时**：``timeout_ms``（YAML: 60000）折算成秒传给沙箱；
  沙箱侧配置（65s）必须**大于** Tool 超时，这样 Tool 层先优雅失败，
  沙箱强杀只作最后兜底 —— 两个超时同时触发会让错误分类失去意义。
- **§34/§35 错误分类**：超时 -> ``ToolTimeout``（可重试），
  非零退出码 -> ``BusinessError``（业务逻辑失败，默认不重试）。

**输出截断**：Tool 内部先把 stdout/stderr 截到 64KB。注意 64KB **大于**
``max_inline_size``（32KB），所以「打印一堆输出」仍然会走 §39/§40 的
artifact 路径 —— 演示大结果时不需要特意构造别的 Tool。
"""
from __future__ import annotations

import sys
from typing import ClassVar, Type

from pydantic import BaseModel, Field

from ..domain.enums import ExecutionMode, IdempotencyLevel, RiskLevel
from ..domain.errors import BusinessError, ToolTimeout
from ..domain.models import ToolMetadata
from .base import BaseTool, ToolContext, ToolOutput

_TOOL_OUTPUT_LIMIT = 64 * 1024
"""Tool 内部截断阈值。刻意**大于** ``max_inline_size``(32KB)：
Tool 只负责「别把内存撑爆」，尺寸分档交给 Result Processor（§37）——
两层各自只做一件事，谁都不越界。"""


def _truncate(text: str, limit: int = _TOOL_OUTPUT_LIMIT) -> str:
    """超长输出截断到 ``limit``，并**保留头尾**与明确标记。

    头尾都留的理由与 §40 的 preview 一致：Python 报错的关键信息在**最后一行**，
    只留头部会把 ``ImportError`` / ``AssertionError`` 这些结论切掉。
    截断标记本身也很重要 —— Agent 必须能区分「程序只输出了这么多」
    和「输出被平台截掉了」，否则它会基于不完整的信息做判断。
    """
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return (
        f"{text[:head]}\n"
        f"...<输出被截断，省略 {omitted} 字符>...\n"
        f"{text[-tail:]}"
    )


class ExecutePythonArgs(BaseModel):
    """参数模型 —— 与 ``configs/tools.yaml`` 的 ``execute_python.params`` 对齐。"""

    code: str = Field(
        min_length=1, max_length=20000, description="要执行的 Python 源码"
    )
    stdin: str = Field(
        default="", max_length=10000, description="喂给标准输入的内容"
    )


class ExecutePythonTool(BaseTool):
    """在沙箱里执行一段 Python（async / estimated 15s / risk=medium）。"""

    name: ClassVar[str] = "execute_python"
    category: ClassVar[str] = "async.sandbox"
    args_model: ClassVar[Type[BaseModel]] = ExecutePythonArgs

    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        name="execute_python",
        version="1.0",
        description=(
            "在受限沙箱中执行一段 Python 代码并返回 exit_code / stdout / stderr。"
            "沙箱默认断网、限制 CPU 与内存，超时会被强制终止。"
        ),
        execution_mode=ExecutionMode.ASYNC,
        estimated_duration_ms=15_000,
        timeout_ms=60_000,
        cpu_limit=2.0,
        memory_limit_mb=1024,
        network_access=False,
        idempotent=True,
        risk_level=RiskLevel.MEDIUM,
        required_permissions=["code.execute"],
        idempotency_level=IdempotencyLevel.IDEMPOTENT,
    )

    # ------------------------------------------------------------------
    def run(self, args: ExecutePythonArgs, ctx: ToolContext) -> ToolOutput:
        """把用户代码交给沙箱执行。

        用 ``[sys.executable, "-c", code]`` 而不是写临时文件再跑：
        ``-c`` 不会在 workspace 里留下痕迹，也不需要额外的文件生命周期管理。
        代价是 traceback 里显示 ``<string>`` 而非文件名 —— 对演示足够。
        """
        timeout_seconds = max(1.0, ctx.metadata.timeout_ms / 1000.0)

        result = ctx.sandbox_run(
            [sys.executable, "-c", args.code],
            cwd=ctx.workspace,
            stdin=args.stdin,
            timeout_seconds=timeout_seconds,
        )

        # ---- §34 错误分类：先分类，再决定谁去恢复 ----
        if result.timed_out:
            # TIMEOUT -> §35 恢复动作 RETRY（可重试），但必须带上超时预算，
            # 否则重试策略无从判断「要不要加大 timeout」。
            raise ToolTimeout(
                f"代码执行超过 {timeout_seconds:.0f}s 被终止",
                detail={
                    "timeout_seconds": timeout_seconds,
                    "stderr": _truncate(result.stderr or ""),
                },
            )

        if result.exit_code != 0:
            # 非零退出码归为**业务错误**：沙箱没坏、平台没坏，是用户代码自己失败了。
            # 与 TIMEOUT 的区别很关键：TIMEOUT 可重试，BUSINESS_ERROR 默认不重试
            # （代码逻辑错了，重跑一万次还是错）。
            raise BusinessError(
                f"代码以非零退出码结束: {result.exit_code}",
                detail={
                    "exit_code": result.exit_code,
                    "stderr": _truncate(result.stderr or ""),
                    "stdout": _truncate(result.stdout or ""),
                },
            )

        stdout = _truncate(result.stdout or "")
        stderr = _truncate(result.stderr or "")
        return ToolOutput(
            data={
                "exit_code": result.exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": result.duration_ms or ctx.elapsed_ms(),
                "timed_out": bool(result.timed_out),
            },
            stats={
                # 原始长度必须留下来：Result Processor 只看到截断后的 64KB，
                # 光看它无法判断「用户到底打印了多少」。
                "stdout_bytes": len((result.stdout or "").encode("utf-8")),
                "stderr_bytes": len((result.stderr or "").encode("utf-8")),
                "tool_truncate_limit": _TOOL_OUTPUT_LIMIT,
                "sandbox_meta": dict(result.meta or {}),
            },
        )
