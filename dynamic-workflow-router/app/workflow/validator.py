"""Workflow 校验器（说明书 §9）。

**校验必须在「保存前」和「执行前」各做一次**（§9.2），因为这两个时刻
要回答的是不同的问题：

    执行前：这份 DAG **现在**能不能跑？（Agent 还在不在、数据流闭不闭合）
    保存前：这份 DAG **将来**还值不值得跑？（预算、风险、是否重复）

只做前者，模板库里会攒下一堆「当时能跑、现在跑不了」的死模板；
只做后者，第一次执行就会撞上运行时的错误。

七项检查（§9.1）：

1. Agent 是否存在且可用
2. 依赖是否指向存在的步骤
3. 是否成环
4. **数据流是否闭合**（每一步的 ``needs`` 都有人产出）
5. 意图要求的能力是否被全覆盖
6. 步数与成本是否在预算内
7. 风险是否可接受（有副作用的步骤必须显式授权）
"""
from __future__ import annotations

from ..domain.enums import RiskLevel
from ..domain.models import (
    Intent,
    RunRequest,
    ValidationIssue,
    ValidationReport,
    WorkflowTemplate,
)
from ..pool.agents import AgentPool


class WorkflowValidator:
    def __init__(self, pool: AgentPool) -> None:
        self.pool = pool

    # ==================================================================
    def validate(
        self,
        template: WorkflowTemplate,
        *,
        intent: Intent | None = None,
        request: RunRequest | None = None,
        provided_keys: list[str] | None = None,
        for_saving: bool = False,
    ) -> ValidationReport:
        report = ValidationReport()
        steps = template.steps
        ids = {step.id for step in steps}

        if not steps:
            report.issues.append(ValidationIssue(code="EMPTY_TEMPLATE",
                                                 message="模板里没有任何步骤"))
            return report

        # ---- 1. Agent 是否存在且可用 ---------------------------------
        for step in steps:
            spec = self.pool.get(step.agent)
            if spec is None:
                report.issues.append(ValidationIssue(
                    code="UNKNOWN_AGENT", step_id=step.id,
                    message=f"{step.id} 引用了池子里不存在的 Agent：{step.agent}",
                ))
            elif not spec.usable:
                report.issues.append(ValidationIssue(
                    code="AGENT_OFFLINE", step_id=step.id,
                    message=f"{step.id} 依赖的 {step.agent} 当前不可用（{spec.health.value}）",
                ))

        # ---- 2. 依赖完整性 -------------------------------------------
        for step in steps:
            for dependency in step.depends_on:
                if dependency not in ids:
                    report.issues.append(ValidationIssue(
                        code="MISSING_DEPENDENCY", step_id=step.id,
                        message=f"{step.id} 依赖的 {dependency} 不存在于本模板",
                    ))

        # ---- 3. 无环 --------------------------------------------------
        cycle = _find_cycle(steps)
        if cycle:
            report.issues.append(ValidationIssue(
                code="CYCLE", step_id=cycle[0],
                message="模板里存在环：" + " → ".join(cycle),
            ))

        # ---- 4. 数据流闭合（§8.3）------------------------------------
        if not cycle:
            available = set(provided_keys or [])
            by_id = {step.id: step for step in steps}
            producer_of: dict[str, str] = {}
            for step in _topological(steps):
                for key in step.needs:
                    if key not in available:
                        report.issues.append(ValidationIssue(
                            code="DATA_FLOW_BROKEN", step_id=step.id,
                            message=(
                                f"{step.id}({step.agent}) 需要 {key!r}，"
                                "但上游没有任何步骤产出它"
                            ),
                        ))
                    # 「闭合」和「有序」是两件事。键有人产出、但产出者不在本步的
                    # 依赖链上，说明这份 DAG 只是**碰巧**按 id 顺序能跑通 ——
                    # 一旦调度器并行执行，这一步就会读到空数据。
                    # 这类 bug 在「先生成再优化顺序」的实现里极常见。
                    elif (producer := producer_of.get(key)) and not _depends_on(
                        step.id, producer, by_id
                    ):
                        report.issues.append(ValidationIssue(
                            code="DATA_FLOW_UNORDERED", step_id=step.id,
                            message=(
                                f"{step.id} 需要 {key!r}，它的产出者是 {producer}，"
                                f"但 {producer} 不在本步的依赖链上 —— "
                                "并行执行时会读到空数据"
                            ),
                        ))
                for key in step.produces:
                    producer_of[key] = step.id
                available |= set(step.produces)
            # 依赖声明与数据流不一致：声明了依赖却没用它的产出（可能是漏写 needs）
            for step in steps:
                for dependency in step.depends_on:
                    upstream = by_id.get(dependency)
                    if upstream is None:
                        continue
                    if not (set(upstream.produces) & set(step.needs)) and step.needs:
                        report.issues.append(ValidationIssue(
                            code="DEPENDENCY_UNUSED", step_id=step.id, severity="warning",
                            message=(
                                f"{step.id} 声明依赖 {dependency}，但没有消费它的任何产出 —— "
                                "可能是漏写了 needs，也可能是多余的依赖"
                            ),
                        ))

        # ---- 5. 能力覆盖 ---------------------------------------------
        if intent is not None and intent.required_capabilities:
            covered: set[str] = set()
            for step in steps:
                spec = self.pool.get(step.agent)
                if spec is not None:
                    covered |= set(spec.capabilities)
            missing = set(intent.required_capabilities) - covered
            if missing:
                report.issues.append(ValidationIssue(
                    code="CAPABILITY_MISSING",
                    message=f"意图要求的能力没有被覆盖：{sorted(missing)}",
                ))

        # ---- 6. 预算 --------------------------------------------------
        if request is not None:
            if len(steps) > request.max_steps:
                report.issues.append(ValidationIssue(
                    code="BUDGET_STEPS",
                    message=f"步数 {len(steps)} 超过上限 {request.max_steps}",
                ))
            cost = template.estimated_cost_total(self.pool)
            if cost > request.max_cost:
                report.issues.append(ValidationIssue(
                    code="BUDGET_COST",
                    message=f"预估成本 {cost} 超过上限 {request.max_cost}",
                ))

        # ---- 7. 风险 --------------------------------------------------
        high_side_effect = [
            step.id for step in steps
            if (spec := self.pool.get(step.agent)) is not None and spec.side_effect
        ]
        if high_side_effect:
            report.issues.append(ValidationIssue(
                code="SIDE_EFFECT_STEP", step_id=high_side_effect[0],
                message=(
                    f"模板包含有副作用的步骤 {high_side_effect} —— "
                    "自动编排不允许把会改环境的动作悄悄插进来"
                ),
            ))
        if request is not None:
            worst = template.max_risk(self.pool)
            if worst.order > request.max_risk.order:
                report.issues.append(ValidationIssue(
                    code="RISK_EXCEEDS_BUDGET",
                    message=f"模板最高风险 {worst.value} 超过上限 {request.max_risk.value}",
                ))

        # ---- 保存前的额外一项：是否与已有模板重复 ----------------------
        if for_saving:
            report.issues.append(ValidationIssue(
                code="INFO_STRUCTURE_HASH", severity="info",
                message=f"结构指纹 {template.structure_hash}（保存时会用它去重）",
            ))

        return report

    # ==================================================================
    def repair(
        self,
        template: WorkflowTemplate,
        *,
        provided_keys: list[str] | None = None,
    ) -> tuple[WorkflowTemplate, ValidationReport]:
        """**能修就修，修不了才报错**（§8.4）。

        排障现场的第一原则同样是「不要一有问题就整份作废」。这里做三件事：

        * 丢掉指向不存在步骤的依赖（多半是模型漏写/写错 id）；
        * 丢掉引用了不存在 Agent 的**可选**步骤；
        * 重排步骤顺序 —— 有环时尝试用数据流重新排序，能解开就不算失败。

        刻意**不修**的两类：引用了不存在 Agent 的**必选**步骤（静默删掉会让
        结论缺一块），以及数据流断裂（那是设计问题，不是笔误）。
        """
        report = ValidationReport()
        # **先深拷贝**：repair 会原地改 depends_on / 删步骤，直接改调用方传进来的
        # 对象会让「原始模板」在调用之后神秘地变了 —— 调用方往往还持有它，
        # 并在后面拿它和修好的版本做对比。
        template = template.model_copy(deep=True)

        kept = []
        for step in template.steps:
            spec = self.pool.get(step.agent)
            if spec is None and step.optional:
                report.repairs.append(f"丢弃可选步骤 {step.id}（Agent {step.agent} 不存在）")
                continue
            kept.append(step)

        kept_ids = {step.id for step in kept}
        for step in kept:
            dangling = [dep for dep in step.depends_on if dep not in kept_ids]
            if dangling:
                step.depends_on = [dep for dep in step.depends_on if dep in kept_ids]
                report.repairs.append(f"移除 {step.id} 的悬空依赖 {dangling}")

        repaired = template.model_copy(update={"steps": kept})

        if _find_cycle(kept):
            reordered = self._reorder_by_dataflow(kept, provided_keys or [])
            if reordered is not None:
                repaired = template.model_copy(update={"steps": reordered})
                report.repairs.append("按数据流重新排序，解开了环")
            else:
                report.issues.append(ValidationIssue(
                    code="CYCLE", message="存在环，且无法按数据流重新排序",
                ))

        return repaired, report

    def _reorder_by_dataflow(self, steps, provided: list[str]):
        """按数据流重建 depends_on —— 能解环就解，解不了返回 None。"""
        available = set(provided)
        remaining = list(steps)
        rebuilt = []
        produced_by: dict[str, str] = {}
        guard = 0
        while remaining and guard < len(steps) + 2:
            guard += 1
            progressed = False
            for step in list(remaining):
                spec = self.pool.get(step.agent)
                needs = set(spec.reads) if spec else set(step.needs)
                if needs.issubset(available):
                    depends = sorted({produced_by[key] for key in needs if key in produced_by})
                    rebuilt.append(step.model_copy(update={"depends_on": depends,
                                                           "needs": sorted(needs)}))
                    for key in (spec.writes if spec else step.produces):
                        produced_by[key] = step.id
                        available.add(key)
                    remaining.remove(step)
                    progressed = True
            if not progressed:
                return None
        return rebuilt if not remaining else None


# ======================================================================
# 图算法
# ======================================================================
def _depends_on(step_id: str, candidate: str, by_id: dict) -> bool:
    """``candidate`` 是否在 ``step_id`` 的依赖闭包里（含自反）。"""
    seen: set[str] = set()
    stack = [step_id]
    while stack:
        current = stack.pop()
        if current == candidate:
            return True
        if current in seen:
            continue
        seen.add(current)
        step = by_id.get(current)
        if step is not None:
            stack.extend(step.depends_on)
    return False


def _topological(steps) -> list:
    by_id = {step.id: step for step in steps}
    remaining = {step.id: set(step.depends_on) for step in steps}
    order = []
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


def _find_cycle(steps) -> list[str] | None:
    graph = {step.id: list(step.depends_on) for step in steps}
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str):
        state = color.get(node, 0)
        if state == 1:
            return [*stack[stack.index(node):], node]
        if state == 2:
            return None
        color[node] = 1
        stack.append(node)
        for neighbour in graph.get(node, []):
            if neighbour not in graph:
                continue
            found = visit(neighbour)
            if found:
                return found
        stack.pop()
        color[node] = 2
        return None

    for node in graph:
        found = visit(node)
        if found:
            return found
    return None


def risk_order(level: RiskLevel) -> int:
    return level.order
