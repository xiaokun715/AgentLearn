"""RunTestTool —— §60 的**异步长任务**主力（说明书 §4.2 / §18 / §50 / §57 / §60 / §62 / §63）。

§60 矩阵第三行：

    ====================  ========  ==========  ==================================
    Tool                  模式      耗时        被用来验证什么
    ====================  ========  ==========  ==================================
    run_test              async     1~5min      异步 + Checkpoint + 崩溃恢复
    ====================  ========  ==========  ==================================

它是「长任务」这条主线的载体：

- **§4.2 异步**：``execution_mode = "async"``。这里的「异步」**不是** Python 的
  ``async/await``，而是「提交后立刻返回 ``job_id``，结果走队列 + 轮询/回调」。
  平台侧进程不需要吊在这儿等 5 分钟 —— 等一会儿没关系，进程重启就全丢了。
- **§62 场景演示**：跑一批 ``TCxxx`` 用例，拿到通过/失败统计。
- **§63 崩溃恢复**：``idempotency_level = IDEMPOTENT``，Worker 崩溃后新 Worker
  可以接管重跑（重跑一次测试不会改变世界）。
- **§18 Checkpoint**：提交后 Agent 把 ``PendingToolCall`` 写进 LangGraph
  Checkpoint，恢复时才知道「有一步没回来」。
- **§50 长任务进度**：每个用例都 ``ctx.emit_progress()``，
  否则 5 分钟的黑盒执行在演示里就是一个「卡住了」的界面。

**演示加速**：真实跑一批测试要几分钟，演示时无法接受。所以每个用例的
「模拟耗时」是确定性的（由用例名 hash 出来，400~2500ms），
再乘一个可配置的**加速系数** ``TOOLPLAT_TEST_SPEED``（默认 ``0.02``）：
默认配置下单个用例真实耗时约 10~50ms，整批两个用例远低于 2 秒。
把该环境变量设为 ``1.0`` 即可恢复「真实时长」的观感。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from typing import Any, ClassVar, Optional, Type

from pydantic import BaseModel, Field

from ..domain.enums import ExecutionMode, IdempotencyLevel, RiskLevel
from ..domain.errors import ToolTimeout
from ..domain.models import ToolMetadata
from .base import BaseTool, ToolContext, ToolOutput

_CASE_ID_RE = re.compile(r"^TC[0-9]{3}$")

DEFAULT_SPEED = 0.02
"""演示加速系数：模拟耗时 1000ms -> 真实 sleep 20ms。"""


def _speed_factor() -> float:
    """从环境变量读加速系数；非法值回退到默认（演示不该被环境变量搞挂）。"""
    raw = os.getenv("TOOLPLAT_TEST_SPEED")
    if raw is None or raw == "":
        return DEFAULT_SPEED
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_SPEED


def _simulated_duration_ms(case: str) -> int:
    """用例的「模拟真实耗时」。

    用用例名做 hash 而不是 ``random``：**确定性**让重复演示、日志比对、
    「同一批用例两次跑结果是否一致」这些验证都能成立。
    """
    digest = hashlib.sha256(case.encode("utf-8")).digest()
    return 400 + int.from_bytes(digest[:2], "big") % 2101  # 400 ~ 2500 ms


def is_failing_case(case: str) -> bool:
    """判定用例是否失败 —— **规则必须简单、明确、可预测**：:

        1. 用例名里含 "FAIL"（不分大小写）-> 失败
        2. 形如 TC\\d{3} 且**编号以 9 结尾**（如 TC999、TC009）-> 失败
        3. 其余一律通过

    为什么用「编号以 9 结尾」而不是随机数：演示要能**指名道姓**地制造失败
    （"把 TC999 加进去看看失败路径"），随机失败会让演示每跑一次换个样子，
    排障与讲解都会被打断。TC999 是行业里约定俗成的「故意失败的用例」。
    """
    if "FAIL" in case.upper():
        return True
    if _CASE_ID_RE.match(case):
        return case[-1] == "9"
    return False


class RunTestArgs(BaseModel):
    """参数模型 —— 与 ``configs/tools.yaml`` 的 ``run_test.params`` 对齐。"""

    test_cases: list[str] = Field(
        min_length=1,
        max_length=50,
        description="用例 ID 列表，形如 TC001；编号以 9 结尾的用例会失败",
    )
    timeout: int = Field(
        default=300,
        ge=1,
        le=600,
        description="整批用例的超时预算（秒）；超过则抛 ToolTimeout",
    )
    path: Optional[str] = Field(
        default=None,
        description="测试工程目录；给出且存在时经沙箱真实执行，否则走模拟",
    )


class RunTestTool(BaseTool):
    """跑一批测试用例（async / estimated 2min / risk=medium / idempotency=idempotent）。"""

    name: ClassVar[str] = "run_test"
    category: ClassVar[str] = "async.execute"
    args_model: ClassVar[Type[BaseModel]] = RunTestArgs

    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        name="run_test",
        version="1.0",
        description=(
            "执行一组测试用例并返回通过/失败统计。"
            "用例 ID 形如 TC001；名字含 FAIL 或编号以 9 结尾的用例会失败。"
            "未提供测试工程目录时走确定性模拟，用于演示异步执行与进度上报。"
        ),
        execution_mode=ExecutionMode.ASYNC,
        estimated_duration_ms=120_000,
        timeout_ms=600_000,
        cpu_limit=4.0,
        memory_limit_mb=4096,
        network_access=False,
        idempotent=True,
        risk_level=RiskLevel.MEDIUM,
        required_permissions=["test.execute"],
        idempotency_level=IdempotencyLevel.IDEMPOTENT,
        # §35：TOOL_NOT_FOUND / 循环降级时的替代 Tool。
        # 它的存在是硬要求 —— FALLBACK 恢复动作找不到这个 Tool 会直接 ToolNotFound 炸掉，
        # 于是「降级」本身成了新的故障源（``run_test_dry_run`` 就是它的落点）。
        fallback_tool="run_test_dry_run",
    )

    # ------------------------------------------------------------------
    def run(self, args: RunTestArgs, ctx: ToolContext) -> ToolOutput:
        """执行用例（真实或模拟）。

        真实执行的门槛刻意设成「``path`` 给了、且确实是个存在的目录、
        且注入了沙箱」三条同时成立：

        - 演示环境里没有真实测试工程，默认必须走模拟，否则演示跑不起来；
        - 没有注入沙箱时 :meth:`ToolContext.sandbox_run` 会拒绝执行 ——
          这是刻意的，**宁可失败也不裸跑**。对 run_test 而言，
          「没沙箱」不构成失败理由（模拟同样能给出结论），
          所以这里退化为模拟而不是抛错，并在 ``stats["mode"]`` 里明示，
          让任何人都不可能把模拟结果误读成真实测试结论。
        """
        started = time.time()
        real_mode = bool(args.path) and os.path.isdir(args.path) and ctx.run_in_sandbox is not None
        if real_mode:
            return self._run_in_sandbox(args, ctx, started)
        return self._run_simulated(args, ctx, started)

    # ------------------------------------------------------------------
    def _run_simulated(self, args: RunTestArgs, ctx: ToolContext, started: float) -> ToolOutput:
        """确定性模拟：每个用例真实 sleep 一点，再按规则判定通过/失败。

        §50：**每个用例上报一次进度**。进度必须来自用例循环内部，
        不能只在开头结尾各报一次 —— 那样「卡在 47% 不动」跟「卡在 0%」
        无法区分，而排障时我们最需要知道的正是「卡在哪一个用例上」。
        """
        speed = _speed_factor()
        total = len(args.test_cases)
        cases: list[dict[str, Any]] = []
        simulated_total_ms = 0

        for index, case in enumerate(args.test_cases, start=1):
            sim_ms = _simulated_duration_ms(case)
            simulated_total_ms += sim_ms
            # 真实耗时 = 模拟耗时 * 加速系数（系数为 0 时不 sleep，供快速回归用）
            if speed > 0:
                time.sleep(sim_ms / 1000.0 * speed)

            failed = is_failing_case(case)
            cases.append(
                {
                    "case": case,
                    "status": "failed" if failed else "passed",
                    "duration_ms": sim_ms,
                }
            )
            ctx.emit_progress(
                index / total * 100.0,
                f"用例 {case} {'FAILED' if failed else 'passed'} ({index}/{total})",
            )

        # §29/§35：超时预算按**模拟耗时**判断（模拟的是「真跑要多久」），
        # 而不是按加速后的真实墙钟 —— 否则测试一加速，超时路径就永远测不到了。
        if simulated_total_ms > args.timeout * 1000:
            raise ToolTimeout(
                f"用例总耗时 {simulated_total_ms}ms 超过 timeout={args.timeout}s 预算",
                detail={"timeout_seconds": args.timeout, "simulated_total_ms": simulated_total_ms},
            )

        return self._summarize(
            cases,
            started,
            mode="simulated",
            speed=speed,
            simulated_total_ms=simulated_total_ms,
        )

    # ------------------------------------------------------------------
    def _run_in_sandbox(self, args: RunTestArgs, ctx: ToolContext, started: float) -> ToolOutput:
        """真实执行：在沙箱里对 ``path`` 跑一次 pytest。

        用例级明细只有模拟模式才有（模拟才知道每个用例的耗时与结论）；
        真实模式给出的是 pytest 的整体结论 + 输出尾部，这是刻意的取舍：
        解析 pytest 输出格式属于「把 Tool 写成一个 pytest 解析器」，
        而本 Tool 的演示目标是**异步链路**，不是测试框架集成。
        """
        argv = [sys.executable, "-m", "pytest", "-q", "--tb=line"]
        result = ctx.sandbox_run(
            argv,
            cwd=args.path,
            timeout_seconds=float(args.timeout),
        )
        if result.timed_out:
            raise ToolTimeout(
                f"pytest 在 {args.timeout}s 内未完成",
                detail={"path": args.path, "argv": argv},
            )

        stdout = (result.stdout or "")[-4000:]
        passed = self._parse_count(stdout, "passed")
        failed = self._parse_count(stdout, "failed")
        total = passed + failed
        return ToolOutput(
            data={
                "total": total,
                "passed": passed,
                "failed": failed,
                "duration_ms": int((time.time() - started) * 1000),
                # 用例明细只有模拟模式能给出；真实模式回报一行 pseudo case，
                # 保证 cases 字段形状稳定（消费方不必写两种分支）。
                "cases": [
                    {
                        "case": "pytest",
                        "status": "passed" if result.exit_code == 0 else "failed",
                        "duration_ms": result.duration_ms,
                    }
                ],
                "summary": stdout.strip().splitlines()[-1] if stdout.strip() else "pytest 无输出",
            },
            stats={
                "mode": "sandbox",
                "exit_code": result.exit_code,
                "path": args.path,
                "note": "用例级明细仅模拟模式提供",
            },
        )

    @staticmethod
    def _parse_count(output: str, keyword: str) -> int:
        """从 pytest 摘要行里抠出 ``N passed`` / ``N failed``。抠不到就当 0。"""
        match = re.search(rf"(\d+)\s+{keyword}", output)
        return int(match.group(1)) if match else 0

    # ------------------------------------------------------------------
    @staticmethod
    def _summarize(
        cases: list[dict[str, Any]],
        started: float,
        *,
        mode: str,
        speed: float,
        simulated_total_ms: int,
    ) -> ToolOutput:
        """汇总成统一的返回结构（模拟与真实模式共用形状）。"""
        passed = sum(1 for c in cases if c["status"] == "passed")
        failed = len(cases) - passed
        return ToolOutput(
            data={
                "total": len(cases),
                "passed": passed,
                "failed": failed,
                "duration_ms": int((time.time() - started) * 1000),
                "cases": cases,
                "summary": f"{passed}/{len(cases)} passed, {failed} failed",
            },
            stats={
                "mode": mode,
                "speed_factor": speed,
                "simulated_total_ms": simulated_total_ms,
            },
        )
