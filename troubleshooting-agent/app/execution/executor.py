"""诊断执行引擎 —— 调度工具、管理状态、执行 Workflow（说明书 §10.2）。

说明书把这一层标成 **「不需要大模型」**：

    | 诊断执行引擎 | 调度工具、管理状态、执行 Workflow | 不需要 |

所以这里全部是确定性代码：拓扑排序、节点级超时、重试、检查点、预算记账。
§10.3 第 3 条要求 ``execute_ready_nodes()`` 支持节点级状态、超时、重试和检查点 ——
本模块逐条实现。

**执行顺序是确定的**：同一批就绪节点按 ID 升序跑，而不是并发跑。
真机上当然可以并行，但演示里顺序执行才能让输出可复现；同时我们会**计算出**
哪些节点本来可以并行，把它打印出来 —— 结论一致，证据可查。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..clock import Clock
from ..domain.enums import NodeStatus, Observation
from ..domain.models import (
    BudgetUsage,
    DiagnosticPlan,
    Evidence,
    NodeResult,
    PlanNode,
)
from ..evidence.store import EvidenceStore
from ..planning.actions import ActionPool
from ..planning.planner import cache_valid
from ..tools.base import ToolCallRecord, ToolContext, ToolError, ToolRegistry


# ======================================================================
# 检查点（§10.3 第 5 条：关键状态在任务恢复后仍然可用）
# ======================================================================
class CheckpointStore:
    """节点级检查点。

    ``persist()`` 在**每个节点之后**调用，写的是「已经完成了哪些节点、产出了什么」。
    真机上应该是数据库 + 对象存储；这里用「可选的文件 + 内存字典」等价替代，
    接口语义一致：按 ``run_id`` 存取，不含任何业务判断。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._memory: dict[str, dict] = {}
        if self.path:
            self.path.mkdir(parents=True, exist_ok=True)

    def save(self, run_id: str, payload: dict) -> None:
        self._memory[run_id] = payload
        if self.path:
            file = self.path / f"{run_id}.json"
            file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

    def load(self, run_id: str) -> dict | None:
        if run_id in self._memory:
            return self._memory[run_id]
        if self.path:
            file = self.path / f"{run_id}.json"
            if file.exists():
                return json.loads(file.read_text(encoding="utf-8"))
        return None


# ======================================================================
# 执行结果
# ======================================================================
@dataclass
class ExecutionOutcome:
    results: list[NodeResult] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    tool_calls: int = 0
    parallelizable: list[list[str]] = field(default_factory=list)

    @property
    def reused(self) -> list[NodeResult]:
        return [item for item in self.results if item.reused_from]

    @property
    def executed(self) -> list[NodeResult]:
        return [item for item in self.results if item.status is NodeStatus.SUCCEEDED
                and not item.reused_from]

    @property
    def failed(self) -> list[NodeResult]:
        return [item for item in self.results
                if item.status in (NodeStatus.FAILED, NodeStatus.TIMED_OUT)]

    def observations(self) -> list[tuple[NodeResult, Observation]]:
        return [
            (item, item.observation)
            for item in self.results
            if item.produced_observation and item.observation is not None
        ]


# ======================================================================
# 执行器
# ======================================================================
class WorkflowExecutor:
    """把一份（已通过校验的）计划跑完。"""

    def __init__(
        self,
        registry: ToolRegistry,
        pool: ActionPool,
        store: EvidenceStore,
        clock: Clock,
        checkpoints: CheckpointStore | None = None,
    ) -> None:
        self.registry = registry
        self.pool = pool
        self.store = store
        self.clock = clock
        self.checkpoints = checkpoints or CheckpointStore()

    # ------------------------------------------------------------------
    def execute(
        self,
        plan: DiagnosticPlan,
        tool_ctx: ToolContext,
        usage: BudgetUsage,
        *,
        run_id: str = "run",
        derived_from: list[str] | None = None,
    ) -> ExecutionOutcome:
        outcome = ExecutionOutcome(parallelizable=self.parallel_groups(plan))
        order = self.topological_order(plan)
        by_id = plan.node_map()

        for node_id in order:
            node = by_id[node_id]
            result = self._run_node(node, tool_ctx, usage, derived_from=derived_from)
            outcome.results.append(result)
            outcome.evidence.extend(self.store.many(result.evidence_ids))
            if not result.reused_from:
                outcome.tool_calls += 1
            # 检查点：每完成一个节点落一次（§10.3 第 3 条）
            self.checkpoints.save(
                run_id,
                {
                    "plan_id": plan.plan_id,
                    "mode": plan.mode.value,
                    "completed": [item.node_id for item in outcome.results],
                    "pending": [nid for nid in order if nid != node_id
                                and nid not in {item.node_id for item in outcome.results}],
                    "usage": usage.as_dict(),
                },
            )
        return outcome

    # ------------------------------------------------------------------
    def _run_node(
        self,
        node: PlanNode,
        tool_ctx: ToolContext,
        usage: BudgetUsage,
        *,
        derived_from: list[str] | None = None,
    ) -> NodeResult:
        spec = self.pool.get(node.skill)

        # ---- 模式 B：复用已有结果，不调工具 ---------------------------
        if node.reused_from:
            return self._reuse(node, tool_ctx)

        if not self.registry.has(node.skill):
            return NodeResult(
                node_id=node.id, skill=node.skill, status=NodeStatus.BLOCKED,
                error=f"没有注册诊断工具：{node.skill}",
            )

        timeout = spec.timeout_seconds if spec else 5.0
        max_attempts = spec.max_attempts if spec else 1
        last_error: str | None = None

        for attempt in range(1, max_attempts + 1):
            started = self.clock.now()
            try:
                observation = self.registry.resolve(node.skill)(tool_ctx, node)
            except ToolError as exc:
                last_error = str(exc)
                elapsed = (self.clock.now() - started) * 1000
                if elapsed > timeout * 1000:
                    return NodeResult(
                        node_id=node.id, skill=node.skill, status=NodeStatus.TIMED_OUT,
                        attempts=attempt, duration_ms=int(elapsed),
                        error=f"节点超时（{elapsed / 1000:.1f}s > {timeout}s）",
                    )
                if attempt >= max_attempts:
                    return NodeResult(
                        node_id=node.id, skill=node.skill, status=NodeStatus.FAILED,
                        attempts=attempt, duration_ms=int(elapsed), error=last_error,
                    )
                continue   # 还有重试机会 —— 节点级重试（§10.3 第 3 条）
            except Exception as exc:  # noqa: BLE001 - 工具是外部边界，必须兜住
                return NodeResult(
                    node_id=node.id, skill=node.skill, status=NodeStatus.FAILED,
                    attempts=attempt, error=f"工具内部异常：{exc}",
                )

            elapsed_ms = int((self.clock.now() - started) * 1000)
            usage.tool_calls += 1
            tool_ctx.record(
                ToolCallRecord(
                    skill=node.skill,
                    outcome=observation.outcome.value,
                    summary=observation.summary,
                    duration_ms=elapsed_ms,
                )
            )

            evidence = self.store.materialize(
                observation.evidence,
                source=node.skill,
                derived_from=derived_from,
            )

            # 超时判定放在**调用之后**：Demo 的工具是瞬时的，
            # 但真机上这里读的是真实墙钟；判据与时机都保持一致。
            if elapsed_ms > timeout * 1000:
                return NodeResult(
                    node_id=node.id, skill=node.skill, status=NodeStatus.TIMED_OUT,
                    observation=observation.outcome, attempts=attempt,
                    duration_ms=elapsed_ms,
                    evidence_ids=[item.evidence_id for item in evidence],
                    error=f"节点超时（{elapsed_ms / 1000:.1f}s > {timeout}s）",
                )

            return NodeResult(
                node_id=node.id, skill=node.skill, status=NodeStatus.SUCCEEDED,
                observation=observation.outcome, attempts=attempt,
                duration_ms=elapsed_ms,
                evidence_ids=[item.evidence_id for item in evidence],
            )

        return NodeResult(
            node_id=node.id, skill=node.skill, status=NodeStatus.FAILED,
            attempts=max_attempts, error=last_error or "未知失败",
        )

    # ------------------------------------------------------------------
    def _reuse(self, node: PlanNode, tool_ctx: ToolContext) -> NodeResult:
        """模式 B：复用缓存里的观测结果，不重新调工具。"""
        cache_key = (node.inputs or {}).get("cache_key")
        entry = tool_ctx.platform.world.context.cached_checks.get(cache_key or "")
        ok, reason = cache_valid(entry or {}, tool_ctx.platform.world.context)
        if not ok or entry is None:
            return NodeResult(
                node_id=node.id, skill=node.skill, status=NodeStatus.SKIPPED,
                skipped_reason=f"缓存不可复用：{reason}",
            )
        return NodeResult(
            node_id=node.id, skill=node.skill, status=NodeStatus.SUCCEEDED,
            observation=Observation(entry["observation"]),
            reused_from=node.reused_from,
            evidence_ids=list(entry.get("evidence_ids", [])),
            attempts=0,
        )

    # ------------------------------------------------------------------
    # 图调度
    # ------------------------------------------------------------------
    @staticmethod
    def topological_order(plan: DiagnosticPlan) -> list[str]:
        """确定性拓扑序：就绪节点里取 ID 最小者。

        没有用「按入度 BFS」的地方是为了可复现 —— 同一份计划每次跑出的
        证据序列必须完全一致，否则演示和回归测试都无从谈起。
        """
        remaining = {node.id: set(node.depends_on) for node in plan.nodes}
        order: list[str] = []
        while remaining:
            ready = sorted(nid for nid, deps in remaining.items() if not deps)
            if not ready:
                break   # 有环（校验器本该拦下）—— 不 crash，交给上层判 FAILED
            for node_id in ready:
                order.append(node_id)
                remaining.pop(node_id)
            for deps in remaining.values():
                deps.difference_update(ready)
        return order

    @staticmethod
    def parallel_groups(plan: DiagnosticPlan) -> list[list[str]]:
        """按依赖层级分组 —— 同一层的节点**在没有资源冲突时**本可并行。

        §6.2 的原话：「并行节点只在它们的输入已经具备、没有互斥资源需求时才能同时执行」。
        所以这里只是把「输入已具备」的部分算出来；资源冲突的判断在校验器里。
        """
        remaining = {node.id: set(node.depends_on) for node in plan.nodes}
        groups: list[list[str]] = []
        while remaining:
            ready = sorted(nid for nid, deps in remaining.items() if not deps)
            if not ready:
                break
            groups.append(ready)
            for node_id in ready:
                remaining.pop(node_id)
            for deps in remaining.values():
                deps.difference_update(ready)
        return groups
