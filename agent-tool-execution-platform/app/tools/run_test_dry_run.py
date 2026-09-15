"""RunTestToolDryRun —— §35 FALLBACK 的落点（说明书 §33 / §35 / §60）。

``configs/tools.yaml`` 里写着::

    run_test:
      fallback_tool: run_test_dry_run

这意味着：当 FALLBACK 恢复动作被触发（Tool Not Found、反复超时、循环降级），
平台会去找一个叫 ``run_test_dry_run`` 的 Tool 顶上去。

**这个 Tool 不存在的话，降级路径本身就是故障源**：恢复策略给出 FALLBACK，
Registry 抛 ``ToolNotFound``，于是「处理失败」的机制制造了新的失败 ——
这正是 §35 强调「降级目标必须真实存在、且语义上真的更弱」的原因。

它的语义是**只做静态检查、绝不执行**：

- 用例名格式是否合法（``TC\\d{3}``）
- ``timeout`` 是否在 1~600 秒之内
- 用例数量是否在 1~50 之间

于是它满足「降级要真的降」这条硬要求：**同样一批输入，它能给出结论，
但不产生任何副作用**。它比 ``run_test`` 弱得多（拿不到通过率），
所以它不能替代正常执行 —— 只能作为「这一步先跳过，把结构性错误告诉 Agent」
的兜底。

权限刻意声明为空：它不碰测试工程、不启进程、无副作用，
于是「Agent 权限不足导致 run_test 失败 -> FALLBACK 又被同一权限拦下」
这种死循环不会发生。**降级路径必须比原路径更容易走通，否则不叫降级。**
"""
from __future__ import annotations

import re
from typing import Any, ClassVar, Type

from pydantic import BaseModel, Field

from ..domain.enums import ExecutionMode, IdempotencyLevel, RiskLevel
from ..domain.errors import ValidationError
from ..domain.models import ToolMetadata
from .base import BaseTool, ToolContext, ToolOutput

_CASE_ID_RE = re.compile(r"^TC[0-9]{3}$")

_MIN_TIMEOUT = 1
_MAX_TIMEOUT = 600
_MAX_CASES = 50


class RunTestDryRunArgs(BaseModel):
    """参数模型 —— 与 ``run_test`` 的 ``test_cases`` / ``timeout`` 保持同形。

    同形是刻意要求：FALLBACK 是「换一个 Tool 干同一件事」，
    如果入参形状不同，恢复策略就得做参数翻译 —— 那又是一处会出错的复杂度。
    （``path`` 不需要：静态检查不读任何文件。）
    """

    test_cases: list[str] = Field(
        default_factory=list,
        max_length=_MAX_CASES,
        description="待校验的用例 ID 列表，形如 TC001",
    )
    timeout: int = Field(
        default=300,
        ge=_MIN_TIMEOUT,
        le=_MAX_TIMEOUT,
        description="超时预算（秒），仅做范围校验，不会真的等待",
    )


class RunTestToolDryRun(BaseTool):
    """测试参数的静态校验（sync / <10ms / risk=low / idempotency=pure）。"""

    name: ClassVar[str] = "run_test_dry_run"
    category: ClassVar[str] = "sync.validate"
    args_model: ClassVar[Type[BaseModel]] = RunTestDryRunArgs

    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        name="run_test_dry_run",
        version="1.0",
        description=(
            "run_test 的降级替代：只静态校验用例 ID 格式与超时范围，"
            "不启动任何进程、不读取测试工程、不产生副作用。"
        ),
        execution_mode=ExecutionMode.SYNC,
        estimated_duration_ms=20,
        timeout_ms=5000,
        cpu_limit=0.2,
        memory_limit_mb=128,
        network_access=False,
        idempotent=True,
        risk_level=RiskLevel.LOW,
        # 纯静态检查，不需要 test.execute：降级路径必须比原路径更容易走通
        required_permissions=[],
        idempotency_level=IdempotencyLevel.PURE,
    )

    # ------------------------------------------------------------------
    def run(self, args: RunTestDryRunArgs, ctx: ToolContext) -> ToolOutput:
        """逐条静态检查，把结论分成 ``validated`` 与 ``invalid`` 两类。

        非法用例**不抛异常**，而是收集进 ``invalid``：这是降级的语义 ——
        降级后的 Tool 应该尽量给出**结论**，而不是把「原 Tool 失败」
        升级成「降级 Tool 也失败」。抛错留给「整批输入根本无法交付」
        的情况（用例数为 0、timeout 越界），那才是真正的校验失败。
        """
        if not args.test_cases:
            raise ValidationError(
                "test_cases 不能为空：静态检查至少需要一个用例",
                detail={"field": "test_cases"},
            )
        if not _MIN_TIMEOUT <= args.timeout <= _MAX_TIMEOUT:
            # Schema 层已经挡过一遍（§21 首轮校验），这里再挡一次是**纵深防御**：
            # FALLBACK 的调用者可能绕过 Gateway 直接构造参数（例如恢复流程自己拼的
            # 修复后参数），而这是本 Tool 唯一能失败的地方，必须自己守住底线。
            raise ValidationError(
                f"timeout 必须在 {_MIN_TIMEOUT}~{_MAX_TIMEOUT} 秒之间，收到 {args.timeout}",
                detail={"field": "timeout", "timeout": args.timeout},
            )

        validated: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for case in args.test_cases:
            reason = self._reject_reason(case)
            if reason is None:
                validated.append({"case": case, "valid": True})
            else:
                invalid.append({"case": case, "valid": False, "reason": reason})

        return ToolOutput(
            data={
                "dry_run": True,
                "validated": validated,
                "invalid": invalid,
                "note": (
                    f"静态检查完成：{len(validated)} 个用例格式合法，"
                    f"{len(invalid)} 个不合法；本次未执行任何测试。"
                ),
            },
            stats={
                "duration_ms": ctx.elapsed_ms(),
                "checked": len(args.test_cases),
                "timeout_seconds": args.timeout,
            },
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _reject_reason(case: str) -> str | None:
        """返回不合法原因；合法则返回 ``None``。"""
        if not isinstance(case, str) or not case:
            return "用例 ID 不能为空"
        if not _CASE_ID_RE.match(case):
            return "用例 ID 必须形如 TC + 3 位数字（例如 TC001）"
        return None
