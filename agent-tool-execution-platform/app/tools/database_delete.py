"""DatabaseDeleteTool —— §60 的**高风险 Tool**（说明书 §51 / §57 / §60 / §67）。

§60 矩阵的最后一行，也是唯一一行「**正常流程里就不该跑起来**」的 Tool：

    ====================  ========  ==========  ==================================
    Tool                  模式      耗时        被用来验证什么
    ====================  ========  ==========  ==================================
    database_delete       async     3s          高风险 -> 人工审批（§51/§67）
    ====================  ========  ==========  ==================================

**它存在的意义就是「不该自动执行」。** 两套机制独立地拦住它：

1. **§51 / §67 风险等级**：``risk_level = HIGH`` -> Risk Engine 直接把它送进
   ``WAITING_HUMAN``，生成审批单，等人批准/拒绝/改参数。
   注意「权限」与「审批」是两件事：``admin_agent`` 有 ``database.delete`` 权限，
   但仍然要过人工 —— 权限回答「能不能」，审批回答「该不该」。
2. **§57 非幂等**：``idempotency_level = NON_IDEMPOTENT``。
   即使审批通过并开始执行，Worker 崩溃后也**不能**自动重跑 ——
   「删了一半」和「一次没删」在数据库里看起来完全不同，而重跑会把前一半再删一遍
   变成「删了两次」。此时只能人工对账。

因此本 Tool 的代码本身**故意写得很笨**：它不做审批、不做权限、不做备份，
只做「给定表与条件，删掉匹配行」。这些责任在平台层，Tool 重复实现一遍
等于制造两套会打架的规则。

**关于 SQL 注入**：语句在 Gateway 层已经被 §24 的注入检测拦过一遍，
这里只做执行。但「上游已经检查过」不能成为下游不做兜底的借口 ——
直接构造出 ``where`` 的调用方可能绕过 Gateway（恢复流程、人工改参数的审批流），
所以这里仍然拒绝**明显危险的整体删除**（``where`` 为空或 ``1=1``）。
"""
from __future__ import annotations

import re
import threading
from typing import Any, ClassVar, Optional, Type

from pydantic import BaseModel, Field

from ..domain.enums import ExecutionMode, IdempotencyLevel, RiskLevel
from ..domain.errors import BusinessError
from ..domain.models import ToolMetadata
from .base import BaseTool, ToolContext, ToolOutput

# ======================================================================
# 模拟表：模块级状态（同一进程内多次调用会真的看到「行变少了」）
# ======================================================================
_DEMO_TABLES: dict[str, list[dict[str, Any]]] = {
    "demo_orders": [
        {"id": 1, "order_no": "ORD1001", "status": "paid", "amount": 128.0},
        {"id": 2, "order_no": "ORD1002", "status": "cancelled", "amount": 64.5},
        {"id": 3, "order_no": "ORD1003", "status": "paid", "amount": 999.0},
        {"id": 4, "order_no": "ORD1004", "status": "cancelled", "amount": 12.0},
        {"id": 5, "order_no": "ORD1005", "status": "pending", "amount": 300.0},
        {"id": 6, "order_no": "ORD1006", "status": "cancelled", "amount": 88.8},
        {"id": 7, "order_no": "ORD1007", "status": "paid", "amount": 420.0},
        {"id": 8, "order_no": "ORD1008", "status": "pending", "amount": 33.3},
        {"id": 9, "order_no": "ORD1009", "status": "cancelled", "amount": 7.5},
        {"id": 10, "order_no": "ORD1010", "status": "paid", "amount": 1500.0},
    ],
    "demo_test_records": [
        {"id": 1, "case_id": "TC001", "result": "passed", "duration_ms": 820},
        {"id": 2, "case_id": "TC002", "result": "passed", "duration_ms": 640},
        {"id": 3, "case_id": "TC009", "result": "failed", "duration_ms": 1150},
        {"id": 4, "case_id": "TC003", "result": "passed", "duration_ms": 910},
        {"id": 5, "case_id": "TC999", "result": "failed", "duration_ms": 2400},
        {"id": 6, "case_id": "TC004", "result": "passed", "duration_ms": 305},
        {"id": 7, "case_id": "TC005", "result": "passed", "duration_ms": 1780},
        {"id": 8, "case_id": "TC006", "result": "passed", "duration_ms": 1220},
        {"id": 9, "case_id": "TC007", "result": "passed", "duration_ms": 460},
        {"id": 10, "case_id": "TC008", "result": "passed", "duration_ms": 1990},
    ],
}
"""模拟表。选择「模块级可变状态」而不是「每次调用都重置」：
删除的意义就在于**可变**，如果下次调用数据又回来了，演示里没人能看出区别。"""

_TABLE_LOCK = threading.Lock()
"""Worker 可能跑在线程里（§14）。删表是个读-改-写序列，
不加锁就会出现「两个 Worker 同时认为只剩 8 行」的幻觉。"""

# 支持极简条件：`字段 运算符 值`
# 运算符含 `==` 只是因为人类常把 SQL 的 `=` 写成 Python 的 `==`，
# 值可以是数字或单双引号字符串。**不支持** AND / OR / IN / LIKE —— 见 _parse_condition。
_CONDITION_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|>=|<=|=|>|<)\s*(.+?)\s*$"
)

_FORBIDDEN_WHERES = {"1=1", "1", "true", "*", "all"}
"""整体删除的常见伪装。归一化（去空格 + 小写）后比对。"""

_CONFIRM_PREFIX = "CONFIRM-DELETE:"
"""审批单里带给操作者的确认口令格式：``CONFIRM-DELETE:{table}``。"""


def reset_demo_tables() -> None:
    """把模拟表恢复到初始状态 —— 反复演示「同一批数据被删掉」时需要它。"""
    with _TABLE_LOCK:
        _DEMO_TABLES["demo_orders"] = [
            {"id": 1, "order_no": "ORD1001", "status": "paid", "amount": 128.0},
            {"id": 2, "order_no": "ORD1002", "status": "cancelled", "amount": 64.5},
            {"id": 3, "order_no": "ORD1003", "status": "paid", "amount": 999.0},
            {"id": 4, "order_no": "ORD1004", "status": "cancelled", "amount": 12.0},
            {"id": 5, "order_no": "ORD1005", "status": "pending", "amount": 300.0},
            {"id": 6, "order_no": "ORD1006", "status": "cancelled", "amount": 88.8},
            {"id": 7, "order_no": "ORD1007", "status": "paid", "amount": 420.0},
            {"id": 8, "order_no": "ORD1008", "status": "pending", "amount": 33.3},
            {"id": 9, "order_no": "ORD1009", "status": "cancelled", "amount": 7.5},
            {"id": 10, "order_no": "ORD1010", "status": "paid", "amount": 1500.0},
        ]
        _DEMO_TABLES["demo_test_records"] = [
            {"id": 1, "case_id": "TC001", "result": "passed", "duration_ms": 820},
            {"id": 2, "case_id": "TC002", "result": "passed", "duration_ms": 640},
            {"id": 3, "case_id": "TC009", "result": "failed", "duration_ms": 1150},
            {"id": 4, "case_id": "TC003", "result": "passed", "duration_ms": 910},
            {"id": 5, "case_id": "TC999", "result": "failed", "duration_ms": 2400},
            {"id": 6, "case_id": "TC004", "result": "passed", "duration_ms": 305},
            {"id": 7, "case_id": "TC005", "result": "passed", "duration_ms": 1780},
            {"id": 8, "case_id": "TC006", "result": "passed", "duration_ms": 1220},
            {"id": 9, "case_id": "TC007", "result": "passed", "duration_ms": 460},
            {"id": 10, "case_id": "TC008", "result": "passed", "duration_ms": 1990},
        ]


class DatabaseDeleteArgs(BaseModel):
    """参数模型 —— 与 ``configs/tools.yaml`` 的 ``database_delete.params`` 对齐。

    ``table`` 的 ``allowed`` 白名单写在 YAML 里（§25），这里只做形状声明；
    真正的白名单校验在 Tool 内再兜一次（见 :meth:`DatabaseDeleteTool.run`）。
    """

    table: str = Field(min_length=1, max_length=64, description="目标表名（必须在白名单内）")
    where: str = Field(
        min_length=1,
        max_length=500,
        description="极简删除条件，形如 \"id > 3\" 或 \"status = 'cancelled'\"",
    )
    confirm_token: str = Field(
        default="",
        max_length=128,
        description="人工审批通过后带出的确认口令，格式 CONFIRM-DELETE:{table}",
    )


class DatabaseDeleteTool(BaseTool):
    """删除模拟表中的匹配行（async / risk=HIGH / idempotency=NON_IDEMPOTENT）。"""

    name: ClassVar[str] = "database_delete"
    category: ClassVar[str] = "async.risky"
    args_model: ClassVar[Type[BaseModel]] = DatabaseDeleteArgs

    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        name="database_delete",
        version="1.0",
        description=(
            "删除演示表中满足条件的记录。高风险且不可逆："
            "调用会被风险引擎拦下并进入人工审批，请勿在无审批的情况下使用。"
        ),
        execution_mode=ExecutionMode.ASYNC,
        estimated_duration_ms=3000,
        timeout_ms=30_000,
        cpu_limit=1.0,
        memory_limit_mb=512,
        network_access=False,
        # False 而不是 True：这里的「幂等」是 §57 意义上的语义，不是「重复调用不出错」
        idempotent=False,
        risk_level=RiskLevel.HIGH,
        required_permissions=["database.write", "database.delete"],
        idempotency_level=IdempotencyLevel.NON_IDEMPOTENT,
    )

    # ------------------------------------------------------------------
    def run(self, args: DatabaseDeleteArgs, ctx: ToolContext) -> ToolOutput:
        """执行删除。

        「只做执行」不等于「不做判断」—— 这里守住的是**本 Tool 自己能独立判断**
        的那部分底线（表名白名单、整体删除、条件可解析），
        平台层的审批与权限不在 Tool 里重复实现。
        """
        if args.table not in _DEMO_TABLES:
            # §25 白名单：表名不可枚举，才轮到「防字符串拼接」；
            # 早白名单晚白名单都要有，这里是最后一道。
            raise BusinessError(
                f"表不在允许列表内: {args.table}",
                detail={"table": args.table, "allowed": sorted(_DEMO_TABLES)},
            )

        # ---- 拒绝整体删除 ----
        normalized = re.sub(r"\s+", "", args.where or "").lower()
        if not normalized:
            raise BusinessError(
                "拒绝执行无条件删除（where 为空）",
                detail={"table": args.table, "where": args.where},
            )
        if normalized in _FORBIDDEN_WHERES:
            raise BusinessError(
                f"拒绝执行整体删除（where={args.where!r} 等价于全表删除）",
                detail={"table": args.table, "where": args.where},
            )

        field, op, expected = self._parse_condition(args.where)
        self._verify_confirm_token(args, ctx)

        with _TABLE_LOCK:
            rows = _DEMO_TABLES[args.table]
            if rows and field not in rows[0] and not any(field in r for r in rows):
                raise BusinessError(
                    f"表中不存在字段: {field}",
                    detail={"table": args.table, "field": field, "columns": sorted(rows[0])},
                )
            survivors = [row for row in rows if not self._matches(row.get(field), op, expected)]
            deleted = len(rows) - len(survivors)
            # 原地替换而不是重新赋值：模块级 dict 的 value 必须被就地改掉，
            # 否则其它持有引用的调用方（比如演示脚本）看不到变化。
            rows[:] = survivors
            remaining = len(rows)

        return ToolOutput(
            data={
                "table": args.table,
                "deleted": deleted,
                "remaining": remaining,
                "where": args.where,
            },
            stats={
                "risk_level": self.metadata.risk_level.value,
                "idempotency_level": self.metadata.idempotency_level.value,
                "confirm_token_verified": bool(args.confirm_token),
                "condition": {"field": field, "op": op, "value": expected},
                "duration_ms": ctx.elapsed_ms(),
            },
        )

    # ==================================================================
    # 极简条件解析 / 求值
    # ==================================================================
    @staticmethod
    def _parse_condition(where: str) -> tuple[str, str, Any]:
        """把 ``"id > 3"`` 解析成 ``("id", ">", 3)``。

        只支持 `字段 运算符 值` 这一种形状。**不支持 AND / OR / IN / LIKE**
        是刻意的取舍：一旦开始支持布尔组合，就等于在 Tool 里重写一个 SQL 解析器，
        而它的正确性没人验证得起 —— 宁可直接拒绝（``BusinessError``，
        §35 表中对应 ABORT：条件写错了不能靠重试修好）。
        """
        match = _CONDITION_RE.match(where)
        if match is None:
            raise BusinessError(
                f"无法解析删除条件（仅支持 \"字段 运算符 值\"）: {where!r}",
                detail={"where": where, "supported_ops": ["=", "==", "!=", ">", "<", ">=", "<="]},
            )
        field, op, raw_value = match.group(1), match.group(2), match.group(3)

        if raw_value[:1] in ("'", '"'):
            if len(raw_value) < 2 or raw_value[-1] != raw_value[0]:
                raise BusinessError(
                    f"字符串值引号不匹配: {raw_value!r}", detail={"where": where}
                )
            return field, ("=" if op == "==" else op), raw_value[1:-1]

        try:
            return field, ("=" if op == "==" else op), int(raw_value)
        except ValueError:
            pass
        try:
            return field, ("=" if op == "==" else op), float(raw_value)
        except ValueError as exc:
            raise BusinessError(
                f"无法识别的条件值（只支持数字与引号字符串）: {raw_value!r}",
                detail={"where": where},
            ) from exc

    @staticmethod
    def _matches(actual: Any, op: str, expected: Any) -> bool:
        """把条件作用到一行上。

        类型不可比时（``status > 3``）**不抛错**、直接判为不匹配：
        条件在语法上合法、只是没有行满足它 —— 这是「删除 0 行」的正常结果，
        不是一个异常。用异常表达「没有匹配」会让 Agent 无法区分
        「条件写错了」和「确实没数据」。
        """
        if actual is None:
            return False
        comparable = (isinstance(actual, (int, float)) and isinstance(expected, (int, float))) or (
            isinstance(actual, str) and isinstance(expected, str)
        )
        if not comparable:
            return op == "!="

        if op == "=":
            return actual == expected
        if op == "!=":
            return actual != expected
        if op == ">":
            return actual > expected
        if op == "<":
            return actual < expected
        if op == ">=":
            return actual >= expected
        if op == "<=":
            return actual <= expected
        return False  # 解析阶段已保证不可达；保留兜底分支而不是 assert

    # ------------------------------------------------------------------
    @staticmethod
    def _verify_confirm_token(args: DatabaseDeleteArgs, ctx: ToolContext) -> None:
        """校验人工审批带出的确认口令。

        **口令为空时放行**是刻意的：真正的闸门是平台的 Risk Engine（§51/§67），
        不是这个字符串。Tool 若在空口令时一律拒绝，就会出现「审批通过了但工具
        说没口令」的荒诞结果 —— 审批流与 Tool 各自实现一半的规则，比都不实现更危险。
        口令一旦提供，就必须对得上，这样「审批单被复用到别的表上」这种改参数
        攻击（§52 MODIFIED 之外的路径）会被挡住。
        """
        if not args.confirm_token:
            ctx.logger.debug("database_delete 未带 confirm_token（依赖平台审批闸门）")
            return
        expected = f"{_CONFIRM_PREFIX}{args.table}"
        if args.confirm_token != expected:
            raise BusinessError(
                "confirm_token 与目标表不匹配，拒绝执行",
                detail={"table": args.table, "expected_format": f"{_CONFIRM_PREFIX}{args.table}"},
            )

    # ------------------------------------------------------------------
    @classmethod
    def expected_confirm_token(cls, table: str) -> str:
        """审批单/演示脚本据此生成口令，避免两边各写一份格式字符串。"""
        return f"{_CONFIRM_PREFIX}{table}"

    @classmethod
    def table_columns(cls, table: str) -> Optional[list[str]]:
        """返回表结构（供审批 UI 展示影响面）。表不存在返回 ``None``。"""
        rows = _DEMO_TABLES.get(table)
        return sorted(rows[0]) if rows else None
