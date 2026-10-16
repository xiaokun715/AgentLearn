"""Workflow 执行（说明书 §10）。

三层结构：

    黑板书（blackboard）  所有步骤共享的 dict，步骤之间**只通过它耦合**
    调度                  按拓扑序执行；同一层的步骤算作并行，耗时取关键路径
    步骤执行              读黑板 -> 调 handler -> 写黑板，带重试与可选跳过

三个刻意的设计：

1. **子 Agent 之间不直接调用**（§10.1）。上游是谁、下游是谁，只有调度器知道。
   这换来的是：同一个 Agent 能被放进任意模板而不需要改一行代码，
   编排器也才能自由重排与并行。
2. **总耗时取关键路径而不是求和**（§10.2）。编排的意义就是让互不依赖的步骤并行，
   求和会把并行的收益算没 —— 于是「编排比直连慢」这个结论会永远是假的。
3. **失败分两种处理**（§10.3）：``optional`` 的步骤失败就跳过并记一笔；
   必选步骤失败先重试，重试完再失败才终止。默默降级成「少做一步」
   是编排系统最危险的失败模式 —— 它会让结论看起来是对的。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import skills
from ..domain.enums import StepStatus
from ..domain.models import (
    Intent,
    StepResult,
    ValidationIssue,
    WorkflowStep,
    WorkflowTemplate,
)
from ..pool.agents import AgentPool


@dataclass
class RuntimeOptions:
    """执行选项。"""

    max_retries: int = 1
    #: 起始时刻（用于把多次运行拼在一条时间线上）
    start_ms: int = 0
    #: 注入失败：``{step_id: 需要失败几次}``，用于演示重试与可选跳过
    fail_steps: dict[str, int] = field(default_factory=dict)
    #: 初始黑板书（query 直接提供的数据）
    initial_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionTrace:
    results: list[StepResult] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    total_ms: int = 0
    parallel_groups: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(
            item.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED)
            for item in self.results
        )


class WorkflowRuntime:
    """黑板书式的 DAG 执行器。"""

    def __init__(self, pool: AgentPool) -> None:
        self.pool = pool

    # ==================================================================
    def execute(
        self,
        template: WorkflowTemplate,
        intent: Intent,
        options: RuntimeOptions | None = None,
    ) -> ExecutionTrace:
        options = options or RuntimeOptions()
        trace = ExecutionTrace(context=dict(options.initial_context))
        by_id = template.step_map()
        order = self._topological(template.steps)
        trace.parallel_groups = self._groups(template.steps)

        finish_at: dict[str, int] = {}
        failure_budget = dict(options.fail_steps)

        for step in order:
            # ---- 上游失败 -> 本步无法执行 ----------------------------
            failed_upstream = [
                dep for dep in step.depends_on
                if dep in by_id and finish_at.get(dep) is None
            ]
            if failed_upstream:
                result = StepResult(
                    id=step.id, agent=step.agent, status=StepStatus.BLOCKED,
                    error=f"上游步骤未成功：{failed_upstream}",
                )
                trace.results.append(result)
                trace.notes.append(f"{step.id} 被上游阻塞：{failed_upstream}")
                continue

            # ---- 起止时刻：取关键路径（§10.2）-----------------------
            start = max(
                (finish_at.get(dep, options.start_ms) for dep in step.depends_on),
                default=options.start_ms,
            )
            result = self._run_step(step, intent, trace.context, start,
                                    options, failure_budget)
            trace.results.append(result)
            # 成功与「可选但被跳过」都要记完成时刻 ——
            # **optional 的语义就是「不做也能继续」**。如果跳过它会让下游全部阻塞，
            # 那它就不是可选的，而是「必选但允许失败」，那是另一回事，
            # 应该由步骤自己的降级逻辑处理，而不是靠 optional 掩盖（§10.3）。
            if result.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED):
                finish_at[step.id] = result.finished_ms
            if result.status is StepStatus.SKIPPED:
                trace.notes.append(
                    f"{step.id} 是可选步骤，失败后跳过；下游照常执行"
                )

        # 总耗时 = 最晚完成时刻（不是各步之和）
        trace.total_ms = max((item.finished_ms for item in trace.results),
                             default=options.start_ms) - options.start_ms
        return trace

    # ==================================================================
    def _run_step(
        self,
        step: WorkflowStep,
        intent: Intent,
        context: dict[str, Any],
        start_ms: int,
        options: RuntimeOptions,
        failure_budget: dict[str, int],
    ) -> StepResult:
        latency = self.pool.latency_of(step.agent)
        attempts = 0
        last_error: str | None = None

        while attempts <= options.max_retries:
            attempts += 1
            # 注入的失败：模拟「这一步本身会抖」
            if failure_budget.get(step.id, 0) > 0:
                failure_budget[step.id] -= 1
                last_error = f"{step.agent} 执行失败（注入）"
                if attempts <= options.max_retries:
                    continue
                break
            try:
                handler = skills.resolve(step.agent)
                produced = handler(context, intent)
            except Exception as exc:  # noqa: BLE001 - 子 Agent 是外部边界
                last_error = f"{step.agent} 抛出异常：{exc}"
                if attempts <= options.max_retries:
                    continue
                break

            context.update(produced)
            return StepResult(
                id=step.id, agent=step.agent, status=StepStatus.SUCCEEDED,
                started_ms=start_ms, finished_ms=start_ms + latency,
                attempts=attempts, produced=produced,
            )

        # 重试用尽
        if step.optional:
            return StepResult(
                id=step.id, agent=step.agent, status=StepStatus.SKIPPED,
                started_ms=start_ms, finished_ms=start_ms + latency,
                attempts=attempts, error=last_error,
            )
        return StepResult(
            id=step.id, agent=step.agent, status=StepStatus.FAILED,
            started_ms=start_ms, finished_ms=start_ms + latency,
            attempts=attempts, error=last_error,
        )

    # ==================================================================
    @staticmethod
    def _topological(steps) -> list[WorkflowStep]:
        by_id = {step.id: step for step in steps}
        remaining = {step.id: set(step.depends_on) for step in steps}
        order: list[WorkflowStep] = []
        while remaining:
            ready = sorted(sid for sid, deps in remaining.items() if not deps)
            if not ready:
                break
            for sid in ready:
                order.append(by_id[sid])
                remaining.pop(sid)
            for deps in remaining.values():
                deps.difference_update(ready)
        return order

    @staticmethod
    def _groups(steps) -> list[list[str]]:
        """按依赖层级分组 —— 同一组内的步骤本可并行。"""
        remaining = {step.id: set(step.depends_on) for step in steps}
        groups: list[list[str]] = []
        while remaining:
            ready = sorted(sid for sid, deps in remaining.items() if not deps)
            if not ready:
                break
            groups.append(ready)
            for sid in ready:
                remaining.pop(sid)
            for deps in remaining.values():
                deps.difference_update(ready)
        return groups


def check_executable(template: WorkflowTemplate, pool: AgentPool) -> list[ValidationIssue]:
    """执行前的最后一道检查 —— 只查「现在能不能跑」。"""
    issues: list[ValidationIssue] = []
    for step in template.steps:
        spec = pool.get(step.agent)
        if spec is None:
            issues.append(ValidationIssue(
                code="UNKNOWN_AGENT", step_id=step.id,
                message=f"{step.agent} 不在池子里",
            ))
        elif not spec.usable:
            issues.append(ValidationIssue(
                code="AGENT_OFFLINE", step_id=step.id,
                message=f"{step.agent} 当前不可用（{spec.health.value}）",
            ))
    return issues
