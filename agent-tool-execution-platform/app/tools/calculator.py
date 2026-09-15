"""CalculatorTool —— §60 能力矩阵里的**最短路径纯函数 Tool**（说明书 §60 / §61）。

它在 §60 那张矩阵里覆盖的是第一行：

    ====================  ========  ========  ==================================
    Tool                  模式      耗时      被用来验证什么
    ====================  ========  ========  ==================================
    calculator            sync      ~50ms     最短路径：纯函数、无副作用、可重放
    ====================  ========  ========  ==================================

具体到各章：

- **§61 场景演示**：``"12345 * 6789"`` -> ``83810205``。
  一个「没有任何不确定性」的调用 —— 正因为它必然成功，
  它才是验证**平台链路**（校验 -> 权限 -> 幂等 -> 调度 -> 结果处理）的好探针：
  这条链路出问题时，错误一定出在平台，而不是出在 Tool。
- **§57 崩溃安全**：``idempotency_level = PURE``。租约过期后新 Worker
  可以直接接管重跑，不需要先查状态 —— 纯函数的结果与执行次数无关。
- **§36 重试语义**：重试这个 Tool 永远不会造成第二次副作用。
- **§26 权限**：``required_permissions = []``，任何 Agent 都能调，
  用于验证「权限充足时不该有任何额外摩擦」。
"""
from __future__ import annotations

import ast
import operator
from typing import Any, ClassVar, Type

from pydantic import BaseModel, Field

from ..domain.enums import ExecutionMode, IdempotencyLevel, RiskLevel
from ..domain.errors import BusinessError, ValidationError
from ..domain.models import ToolMetadata
from .base import BaseTool, ToolContext, ToolOutput


class CalculatorArgs(BaseModel):
    """参数模型 —— 与 ``configs/tools.yaml`` 的 ``calculator.params`` 对齐。

    ``max_length: 512`` 是 YAML 里的硬约束，这里再声明一次是为了让
    Schema 层就直接挡住超长表达式（§21），而不是等到 AST 解析时才炸。
    """

    expression: str = Field(
        min_length=1,
        max_length=512,
        description="四则运算表达式，如 \"12345 * 6789\"；不支持变量与函数调用",
    )


# ---- 允许的语法白名单 ------------------------------------------------
#
# 白名单而不是黑名单：黑名单永远列不完（``__import__`` / ``getattr`` /
# ``().__class__.__bases__`` ...），而白名单只有 9 个节点。
# 安全上的「确定性兜底」在这里就是「默认拒绝」：没在白名单里的节点一律报错。
_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_MAX_POW_EXPONENT = 1000
"""``**`` 是唯一的「资源放大器」：``2 ** 100000000`` 只花 20 个字符就能让
CPU 跑满、内存爆掉（§27 Resource Exhausted 的经典触发方式）。
表达式长度限制拦不住它，所以对指数单独设上限 —— 纯函数的正确性不该
依赖调用方「不写坏表达式」的善意。"""


class CalculatorTool(BaseTool):
    """安全四则运算器（sync / ~50ms / risk=low / idempotency=pure）。"""

    name: ClassVar[str] = "calculator"
    category: ClassVar[str] = "sync.pure"
    args_model: ClassVar[Type[BaseModel]] = CalculatorArgs

    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        name="calculator",
        version="1.0",
        description=(
            "计算一个只含数字与 + - * / // % ** 以及括号的算术表达式。"
            "不支持变量、函数调用与任何名字，纯函数，可安全重放。"
        ),
        execution_mode=ExecutionMode.SYNC,
        estimated_duration_ms=50,
        timeout_ms=2000,
        cpu_limit=0.5,
        memory_limit_mb=128,
        network_access=False,
        idempotent=True,
        risk_level=RiskLevel.LOW,
        required_permissions=[],
        idempotency_level=IdempotencyLevel.PURE,
    )

    # ------------------------------------------------------------------
    def run(self, args: CalculatorArgs, ctx: ToolContext) -> ToolOutput:
        """求值表达式。

        **绝不使用 ``eval()`` / ``exec()`` / ``compile(mode="eval")``**：
        ``eval`` 的执行环境里 ``__builtins__`` 是可及的，
        一个 ``__import__('os').system('rm -rf /')`` 就能击穿整个沙箱前提
        （Tool 代码与平台代码跑在同一个解释器里，§27 的隔离是针对子进程的）。
        AST 白名单求值只走「读一遍语法树 + 递归算数」，攻击面被压到
        「节点种类」这一个维度上。
        """
        node = self._parse(args.expression)
        result = self._evaluate(node)

        # 整除/取模能把 float 结果带回来，统一转成「人的直觉形状」：
        # 整数值的 float 显示成 int（3.0 -> 3），但不强转非整数结果。
        if isinstance(result, float) and result.is_integer():
            result = int(result)

        return ToolOutput(
            data={"expression": args.expression, "result": result},
            stats={"duration_ms": ctx.elapsed_ms(), "expression_length": len(args.expression)},
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _parse(expression: str) -> ast.expr:
        """解析成 AST，语法错误直接归为 ``VALIDATION_ERROR``（§34）。

        归成校验错误而不是业务错误很重要：校验错误的恢复动作是
        ``REPAIR``（§35）—— 让参数自愈去修 ``"12345 * * 6789"`` 这类笔误，
        而不是让整个 workflow 直接失败。
        """
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ValidationError(
                f"表达式语法错误: {expression!r}",
                detail={"expression": expression, "reason": str(exc)},
            ) from exc
        return tree.body

    @classmethod
    def _evaluate(cls, node: ast.AST) -> Any:
        """递归求值；**未经白名单批准的节点一律拒绝**。"""
        if isinstance(node, ast.Constant):
            # 只放行 int / float：bool 是 int 的子类（True 会被当成 1），
            # 复杂常量（None / Ellipsis / bytes）没有算术意义，一律拒绝。
            value = node.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise cls._reject(node)
            return value

        if isinstance(node, ast.BinOp):
            op = _BIN_OPS.get(type(node.op))
            if op is None:
                raise cls._reject(node.op)
            left = cls._evaluate(node.left)
            right = cls._evaluate(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > _MAX_POW_EXPONENT:
                    raise ValidationError(
                        f"幂运算指数过大（>{_MAX_POW_EXPONENT}），拒绝执行",
                        detail={"exponent": right},
                    )
            try:
                return op(left, right)
            except ZeroDivisionError as exc:
                # 除零**不是**校验错误：表达式完全合法，只是业务上无意义。
                # 归为 BUSINESS_ERROR -> §35 表中对应 ABORT（不重试，
                # 因为重试一万次还是除零）。
                raise BusinessError(
                    "除数不能为 0",
                    detail={"expression_node": ast.dump(node)},
                ) from exc
            except OverflowError as exc:
                raise ValidationError(
                    "数值溢出，表达式结果超出可表示范围",
                    detail={"node": ast.dump(node)},
                ) from exc

        if isinstance(node, ast.UnaryOp):
            op = _UNARY_OPS.get(type(node.op))
            if op is None:
                raise cls._reject(node.op)
            return op(cls._evaluate(node.operand))

        # Name / Call / Attribute / Subscript / Lambda / 推导式 ...
        # 全部落到这里 —— 这是「默认拒绝」的具体形态。
        raise cls._reject(node)

    @staticmethod
    def _reject(node: ast.AST) -> ValidationError:
        """构造一个说明清楚的拒绝错误：告诉调用方**哪一个**节点不被支持。"""
        kind = type(node).__name__
        return ValidationError(
            f"表达式包含不被允许的语法节点: {kind}",
            detail={
                "node_type": kind,
                "allowed": sorted(
                    {"Constant", "BinOp", "UnaryOp"}
                    | {t.__name__ for t in _BIN_OPS}
                    | {t.__name__ for t in _UNARY_OPS}
                ),
            },
        )
