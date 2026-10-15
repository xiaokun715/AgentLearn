"""Workflow Validator —— 确定性校验，**不能只依赖 Prompt 约束**（说明书 §6.3 / §10.3）。

说明书 §6.3 的第一条禁令非常明确：

> 不要让 LLM 直接生成任意 Python 代码并执行。
> 更安全的设计是让模型只输出结构化的节点计划。

即便如此，结构化计划仍然可能是坏的：有环、引用了不存在的 skill、
偷偷塞进一个重启设备的节点、或者并行拉起两台互相冲突的环境。
所以校验器必须由确定性代码执行，逐条检查说明书列出的八项：

1. Skill 是否存在
2. 输入输出 Schema 是否匹配
3. 依赖关系是否完整
4. DAG 是否存在环
5. 是否满足权限要求
6. 是否存在资源冲突
7. 是否超过节点数量、调用次数和总耗时预算
8. 是否包含未授权的恢复操作

任何一条判为 ``blocking``，计划就不执行 —— 而不是「打个警告然后照跑」。
"""
from __future__ import annotations

from ..domain.enums import RiskTier
from ..domain.models import (
    Budget,
    BudgetUsage,
    DiagnosticPlan,
    FailureContext,
    ValidationIssue,
    ValidationReport,
)
from .actions import ActionPool


class WorkflowValidator:
    """诊断计划的静态校验器。"""

    def __init__(
        self,
        pool: ActionPool,
        *,
        permissions: set[str] | None = None,
        granted_permissions: set[str] | None = None,
    ) -> None:
        self.pool = pool
        self.permissions = permissions or set()
        #: 通过人工审批临时获得的权限（§7.2 授权策略）。它**不**能解锁 R4。
        self.granted_permissions = granted_permissions or set()

    # ------------------------------------------------------------------
    def validate(
        self,
        plan: DiagnosticPlan,
        ctx: FailureContext | None = None,
        *,
        budget: Budget | None = None,
        usage: BudgetUsage | None = None,
        allow_recovery: bool = False,
    ) -> ValidationReport:
        issues: list[ValidationIssue] = []
        budget = budget or Budget()
        usage = usage or BudgetUsage()

        # ---- 0. 空计划 / 节点 ID 重复 ---------------------------------
        if not plan.nodes:
            issues.append(
                ValidationIssue(code="EMPTY_PLAN", message="计划里没有任何节点")
            )
            return ValidationReport(issues=issues)

        seen: set[str] = set()
        for node in plan.nodes:
            if node.id in seen:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_NODE_ID",
                        node_id=node.id,
                        message=f"节点 ID 重复：{node.id}（节点 ID 必须是计划内唯一的）",
                    )
                )
            seen.add(node.id)

        node_ids = {node.id for node in plan.nodes}

        # ---- 1. Skill 是否存在 + 5/8. 权限与恢复授权 -------------------
        for node in plan.nodes:
            spec = self.pool.get(node.skill)
            if spec is None:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_SKILL",
                        node_id=node.id,
                        message=f"动作池里不存在 skill={node.skill!r}",
                    )
                )
                continue

            # 8. 未授权的恢复操作混进诊断计划
            if spec.kind == "recovery" and not allow_recovery:
                issues.append(
                    ValidationIssue(
                        code="UNAUTHORIZED_RECOVERY",
                        node_id=node.id,
                        message=(
                            f"{node.skill} 是恢复动作（{spec.tier.value}），"
                            "不允许出现在诊断阶段的计划里 —— 诊断与恢复必须分开"
                        ),
                    )
                )

            # R4 在任何阶段都禁止自动执行
            if spec.tier is RiskTier.R4:
                issues.append(
                    ValidationIssue(
                        code="FORBIDDEN_RECOVERY",
                        node_id=node.id,
                        message=(
                            f"{node.skill} 属于 R4（默认禁止自动执行）："
                            "改测试预期 / 覆盖正式配置 / 不可逆操作不在自动通道内"
                        ),
                    )
                )

            # 5. 权限：默认权限 + 人工审批临时授予的权限
            if spec.permission:
                allowed = self.permissions | self.granted_permissions
                if spec.permission not in allowed:
                    issues.append(
                        ValidationIssue(
                            code="PERMISSION_DENIED",
                            node_id=node.id,
                            message=(
                                f"{node.skill} 需要权限 {spec.permission}，"
                                f"当前持有 {sorted(allowed) or '（空）'}；"
                                "R3 及以上需要先走人工审批"
                            ),
                        )
                    )

        # ---- 3. 依赖关系完整 + 2. 输入输出契约 -------------------------
        for node in plan.nodes:
            for dependency in node.depends_on:
                if dependency not in node_ids:
                    issues.append(
                        ValidationIssue(
                            code="MISSING_DEPENDENCY",
                            node_id=node.id,
                            message=f"依赖 {dependency!r} 不存在于本计划",
                        )
                    )
            source = (node.inputs or {}).get("from")
            if source and source not in node.depends_on:
                issues.append(
                    ValidationIssue(
                        code="SCHEMA_MISMATCH",
                        node_id=node.id,
                        message=(
                            f"{node.skill} 声明消费 {source} 的输出，"
                            "但没有把它写进 depends_on —— 输入契约与依赖图不一致"
                        ),
                    )
                )

        # ---- 4. DAG 是否有环 ------------------------------------------
        cycle = _find_cycle(plan)
        if cycle:
            issues.append(
                ValidationIssue(
                    code="CYCLE_DETECTED",
                    node_id=cycle[0],
                    message="计划中存在环：" + " → ".join(cycle),
                )
            )

        # ---- 6. 资源冲突 ----------------------------------------------
        for index, node in enumerate(plan.nodes):
            spec = self.pool.get(node.skill)
            if spec is None or not spec.resource:
                continue
            for other in plan.nodes[index + 1:]:
                other_spec = self.pool.get(other.skill)
                if other_spec is None or other_spec.resource != spec.resource:
                    continue
                if node.id in _reachable(other.id, plan) or other.id in _reachable(node.id, plan):
                    continue  # 有依赖关系则是串行，不冲突
                issues.append(
                    ValidationIssue(
                        code="RESOURCE_CONFLICT",
                        node_id=node.id,
                        message=(
                            f"{node.skill} 与 {other.skill} 争抢互斥资源 "
                            f"{spec.resource!r} 却互不依赖 —— 不能并行执行"
                        ),
                    )
                )

        # ---- 7. 预算 --------------------------------------------------
        if len(plan.nodes) > budget.max_nodes_per_plan:
            issues.append(
                ValidationIssue(
                    code="BUDGET_NODE_COUNT",
                    message=(
                        f"计划节点数 {len(plan.nodes)} 超过单轮上限 "
                        f"{budget.max_nodes_per_plan}"
                    ),
                )
            )
        projected = usage.tool_calls + len(plan.nodes)
        if projected > budget.max_tool_calls:
            issues.append(
                ValidationIssue(
                    code="BUDGET_TOOL_CALLS",
                    message=(
                        f"执行本计划后工具调用数将达到 {projected}，"
                        f"超过总量上限 {budget.max_tool_calls}"
                    ),
                )
            )

        return ValidationReport(issues=issues)

    # ------------------------------------------------------------------
    def sanitize(
        self,
        plan: DiagnosticPlan,
        ctx: FailureContext | None = None,
        **kwargs,
    ) -> tuple[DiagnosticPlan, ValidationReport]:
        """校验并**剔除**非法节点，返回可以执行的计划。

        排障现场的取舍：整份计划作废太浪费（一个越权节点不该毁掉整轮诊断），
        所以这里做的是「切掉坏节点 + 保留告警」。但如果剔除后计划空了，
        调用方必须当成失败处理（见 agent 的下一轮处理）。
        """
        report = self.validate(plan, ctx, **kwargs)
        bad_nodes = {issue.node_id for issue in report.blocking if issue.node_id}
        global_blocking = [issue for issue in report.blocking if not issue.node_id]
        if global_blocking:
            return plan.model_copy(update={"nodes": []}), report
        kept = [node for node in plan.nodes if node.id not in bad_nodes]
        # 被剔除节点的下游也要剔除（依赖已经不存在）
        kept_ids = {node.id for node in kept}
        kept = [node for node in kept if all(dep in kept_ids for dep in node.depends_on)]
        return plan.model_copy(update={"nodes": kept}), report

    # ------------------------------------------------------------------
    def permissions_for(self, plan: DiagnosticPlan) -> set[str]:
        """计划需要的全部权限（用于审批展示）。"""
        needed: set[str] = set()
        for node in plan.nodes:
            spec = self.pool.get(node.skill)
            if spec is not None and spec.permission:
                needed.add(spec.permission)
        return needed


# ======================================================================
# 图算法（纯函数，便于单测）
# ======================================================================
def _find_cycle(plan: DiagnosticPlan) -> list[str] | None:
    """DFS 找环，返回环上的节点序列；无环返回 ``None``。"""
    graph = {node.id: [dep for dep in node.depends_on] for node in plan.nodes}
    color: dict[str, int] = {}          # 0=未访问 1=在栈上 2=已完成
    stack: list[str] = []

    def visit(node_id: str) -> list[str] | None:
        state = color.get(node_id, 0)
        if state == 1:
            index = stack.index(node_id)
            return [*stack[index:], node_id]
        if state == 2:
            return None
        color[node_id] = 1
        stack.append(node_id)
        for neighbour in graph.get(node_id, []):
            if neighbour not in graph:
                continue
            found = visit(neighbour)
            if found:
                return found
        stack.pop()
        color[node_id] = 2
        return None

    for node_id in graph:
        found = visit(node_id)
        if found:
            return found
    return None


def _reachable(start: str, plan: DiagnosticPlan) -> set[str]:
    """从 ``start`` 出发能到达的节点（沿依赖方向向上游走）。"""
    graph = {node.id: list(node.depends_on) for node in plan.nodes}
    seen: set[str] = set()
    queue = [start]
    while queue:
        current = queue.pop()
        for neighbour in graph.get(current, []):
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return seen
