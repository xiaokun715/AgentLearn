"""执行引擎测试（说明书 §6.3 / §10.3 第 3 条）。

节点级状态、超时、重试、检查点、模式 B 复用、确定性拓扑序 —— 这些都属于
「不需要大模型」的那一层，所以测试也是最直接的：给一个计划，看执行结果。
"""
from __future__ import annotations

from app.clock import ManualClock
from app.domain.enums import NodeStatus, Observation
from app.domain.models import BudgetUsage, DiagnosticPlan, PlanNode, ToolObservation
from app.execution.executor import CheckpointStore


def _plan(*nodes: tuple[str, str, list[str]]) -> DiagnosticPlan:
    return DiagnosticPlan(
        nodes=[PlanNode(id=nid, skill=skill, depends_on=deps) for nid, skill, deps in nodes]
    )


# ======================================================================
# 调度
# ======================================================================
def test_topological_order_is_deterministic(env_platform):
    plan = _plan(
        ("N3", "analyze_logs", ["N1"]),
        ("N1", "inspect_config", []),
        ("N2", "check_environment_state", []),
    )
    order = env_platform.executor.topological_order(plan)
    assert order == ["N1", "N2", "N3"]


def test_parallel_groups_split_by_dependency_level(env_platform):
    plan = _plan(
        ("N1", "inspect_config", []),
        ("N2", "check_environment_state", []),
        ("N3", "analyze_logs", ["N1", "N2"]),
    )
    assert env_platform.executor.parallel_groups(plan) == [["N1", "N2"], ["N3"]]


def test_cycle_does_not_hang_the_scheduler(env_platform):
    plan = _plan(
        ("N1", "inspect_config", ["N2"]),
        ("N2", "analyze_logs", ["N1"]),
    )
    assert env_platform.executor.topological_order(plan) == []


# ======================================================================
# 执行
# ======================================================================
def test_execute_collects_observations_and_evidence(env_platform):
    plan = _plan(("N1", "check_environment_state", []))
    usage = BudgetUsage()
    outcome = env_platform.executor.execute(
        plan, env_platform.tool_ctx, usage, run_id="t"
    )
    assert outcome.tool_calls == 1
    assert usage.tool_calls == 1
    assert outcome.results[0].status is NodeStatus.SUCCEEDED
    assert outcome.results[0].observation is Observation.ANOMALY
    assert outcome.results[0].evidence_ids
    assert {item.source for item in outcome.evidence} == {"check_environment_state"}


def test_unknown_skill_is_blocked_not_crashed(env_platform):
    plan = _plan(("N1", "shell_exec", []))
    outcome = env_platform.executor.execute(
        plan, env_platform.tool_ctx, BudgetUsage(), run_id="t"
    )
    assert outcome.results[0].status is NodeStatus.BLOCKED


def test_tool_error_is_reported_as_failed(env_platform):
    from app.tools.base import ToolError

    def boom(ctx, node):
        raise ToolError("接口 500")

    env_platform.registry.add("inspect_config", boom)
    plan = _plan(("N1", "inspect_config", []))
    outcome = env_platform.executor.execute(
        plan, env_platform.tool_ctx, BudgetUsage(), run_id="t"
    )
    assert outcome.results[0].status is NodeStatus.FAILED
    assert "接口 500" in (outcome.results[0].error or "")


def test_node_retry_uses_declared_max_attempts(env_platform):
    from app.tools.base import ToolError

    calls = {"n": 0}

    def flaky(ctx, node):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ToolError("第一次抖动")
        return ToolObservation(skill="inspect_config", outcome=Observation.NORMAL)

    env_platform.registry.add("inspect_config", flaky)
    plan = _plan(("N1", "inspect_config", []))
    outcome = env_platform.executor.execute(
        plan, env_platform.tool_ctx, BudgetUsage(), run_id="t"
    )
    assert outcome.results[0].status is NodeStatus.SUCCEEDED
    assert outcome.results[0].attempts == 2
    assert calls["n"] == 2


def test_node_timeout_is_detected_via_injected_clock(make_platform, manual_clock):
    """超时判据读的是可注入时钟 —— 测试不必真的等 60 秒。"""
    platform = make_platform("env", clock=manual_clock)

    def slow(ctx, node):
        ctx.clock.advance(999)       # 手动时钟：把表拨快 999 秒
        return ToolObservation(skill="inspect_config", outcome=Observation.NORMAL)

    platform.registry.add("inspect_config", slow)
    plan = _plan(("N1", "inspect_config", []))
    outcome = platform.executor.execute(
        plan, platform.tool_ctx, BudgetUsage(), run_id="t"
    )
    assert outcome.results[0].status is NodeStatus.TIMED_OUT
    assert "超时" in (outcome.results[0].error or "")


# ======================================================================
# 模式 B：复用
# ======================================================================
def test_reused_node_does_not_call_the_tool(env_platform, cache_entry):
    ctx = env_platform.world.context
    ctx.cached_checks = {"environment_state": cache_entry(Observation.ANOMALY)}
    plan = DiagnosticPlan(
        nodes=[
            PlanNode(
                id="N1",
                skill="check_environment_state",
                inputs={"cache_key": "environment_state"},
                reused_from="cache:environment_state",
            )
        ]
    )
    usage = BudgetUsage()
    outcome = env_platform.executor.execute(plan, env_platform.tool_ctx, usage, run_id="t")
    assert outcome.tool_calls == 0
    assert usage.tool_calls == 0
    assert outcome.results[0].reused_from == "cache:environment_state"
    assert outcome.results[0].observation is Observation.ANOMALY


def test_reuse_is_skipped_when_cache_is_invalid(env_platform, cache_entry):
    ctx = env_platform.world.context
    ctx.cached_checks = {
        "environment_state": cache_entry(Observation.ANOMALY, age_minutes=999)
    }
    plan = DiagnosticPlan(
        nodes=[
            PlanNode(
                id="N1",
                skill="check_environment_state",
                inputs={"cache_key": "environment_state"},
                reused_from="cache:environment_state",
            )
        ]
    )
    outcome = env_platform.executor.execute(
        plan, env_platform.tool_ctx, BudgetUsage(), run_id="t"
    )
    assert outcome.results[0].status is NodeStatus.SKIPPED
    assert "过期" in (outcome.results[0].skipped_reason or "")


# ======================================================================
# 检查点
# ======================================================================
def test_checkpoint_is_written_after_each_node(env_platform):
    plan = _plan(
        ("N1", "inspect_config", []),
        ("N2", "analyze_logs", []),
    )
    env_platform.executor.execute(plan, env_platform.tool_ctx, BudgetUsage(), run_id="ck")
    saved = env_platform.executor.checkpoints.load("ck")
    assert saved is not None
    assert saved["completed"] == ["N1", "N2"]
    assert saved["pending"] == []
    assert saved["usage"]["tool_calls"] == 2


def test_checkpoint_store_persists_to_disk(tmp_path):
    store = CheckpointStore(tmp_path / "ck")
    store.save("run_x", {"step": 1})
    assert (tmp_path / "ck" / "run_x.json").exists()

    fresh = CheckpointStore(tmp_path / "ck")
    assert fresh.load("run_x") == {"step": 1}
    assert fresh.load("missing") is None


def test_checkpoint_without_path_stays_in_memory():
    store = CheckpointStore()
    store.save("run_y", {"step": 2})
    assert store.load("run_y") == {"step": 2}


# ======================================================================
# 预算
# ======================================================================
def test_budget_reports_first_exhausted_resource():
    from app.domain.models import Budget

    budget = Budget(max_steps=1, max_tool_calls=9, max_seconds=100, max_recovery_attempts=9)
    usage = BudgetUsage(steps=1)
    assert usage.exhausted(budget) == "max_steps"
    usage2 = BudgetUsage(tool_calls=9)
    assert usage2.exhausted(budget) == "max_tool_calls"
    assert BudgetUsage().exhausted(budget) is None


def test_manual_clock_advances_without_waiting():
    clock = ManualClock(start=100.0)
    assert clock.now() == 100.0
    clock.advance(3.5)
    assert clock.now() == 103.5
    clock.sleep(1.0)          # 手动时钟下「睡」= 拨表，不消费真实时间
    assert clock.now() == 104.5
