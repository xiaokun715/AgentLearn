"""规划器与校验器测试（说明书 §5.2 / §5.3 / §6.2 / §6.3）。

两块重点：
- **规划器**选的是「最能区分假设且代价可接受」的动作，且优先模板；
- **校验器**把模型可能写出的七类坏计划全部拦下，同时不误伤合法计划。
"""
from __future__ import annotations

import pytest

from app.classification.classifier import FailureClassifier
from app.domain.enums import FailureCategory, Observation, PlanMode
from app.domain.models import DiagnosticPlan, FailureClassification, PlanNode
from app.domain.policy import template_for
from app.hypothesis.manager import HypothesisManager


def _manager(platform, world_category: FailureCategory | None = None) -> HypothesisManager:
    if world_category is None:
        agent = platform.build_agent()
        trace = agent.analyzer.analyze(platform.world.context)
        classification = FailureClassifier(llm=platform.llm).classify(
            platform.world.context, trace
        )
    else:
        classification = FailureClassification(primary_category=world_category)
    return HypothesisManager.from_classification(classification)


def _llm_plan(platform, style: str, manager: HypothesisManager) -> DiagnosticPlan:
    platform.llm.plan_style = style
    platform.planner.llm = platform.llm
    plan = platform.planner.build_from_llm(platform.world.context, manager)
    assert plan is not None
    return plan


# ======================================================================
# 规划器：评分与选动作
# ======================================================================
def test_planner_picks_highest_score_not_most_related(env_platform):
    manager = _manager(env_platform)
    ranked = env_platform.planner.rank(manager)
    assert ranked[0].spec.name == "check_environment_state"
    assert ranked[0].score >= ranked[1].score


def test_score_divides_by_cost_and_risk(env_platform):
    manager = _manager(env_platform)
    spec = env_platform.pool.require("analyze_logs")
    scored = env_platform.planner.score(spec, manager)
    from app.domain.policy import SCORING

    expected = scored.information_gain / (
        spec.cost + SCORING.risk_weight * spec.risk + SCORING.epsilon
    )
    assert scored.score == pytest.approx(expected)


def test_uninformative_action_is_excluded_from_candidates(env_platform):
    manager = _manager(env_platform)
    names = [item.spec.name for item in env_platform.planner.rank(manager)]
    assert "retrieve_failure_cases" not in names


def test_already_executed_action_is_excluded(env_platform):
    manager = _manager(env_platform)
    names = [
        item.spec.name
        for item in env_platform.planner.rank(manager, exclude={"check_environment_state"})
    ]
    assert "check_environment_state" not in names


def test_recovery_actions_never_appear_in_diagnostic_ranking(env_platform):
    manager = _manager(env_platform)
    names = [item.spec.name for item in env_platform.planner.rank(manager)]
    assert not set(names) & {spec.name for spec in env_platform.pool.recovery()}


# ======================================================================
# 模式 A / B：模板与裁剪
# ======================================================================
def test_predefined_template_is_used_on_first_round(env_platform):
    plan, mode = env_platform.planner.build_from_template(
        env_platform.world.context, template_for(FailureCategory.ENVIRONMENT)
    )
    assert mode is PlanMode.PREDEFINED
    assert [node.skill for node in plan.nodes][0] == "check_environment_state"
    assert plan.template_id == "TPL_ENV_FIRST"


def test_fresh_cache_turns_template_into_trimmed(env_platform, cache_entry):
    ctx = env_platform.world.context
    ctx.cached_checks = {"environment_state": cache_entry(Observation.ANOMALY)}
    plan, mode = env_platform.planner.build_from_template(
        ctx, template_for(FailureCategory.ENVIRONMENT)
    )
    assert mode is PlanMode.TRIMMED
    reused = [node for node in plan.nodes if node.reused_from]
    assert [node.skill for node in reused] == ["check_environment_state"]


def test_stale_cache_is_not_reused(env_platform, cache_entry):
    ctx = env_platform.world.context
    ctx.cached_checks = {
        "environment_state": cache_entry(Observation.ANOMALY, age_minutes=120)
    }
    plan, mode = env_platform.planner.build_from_template(
        ctx, template_for(FailureCategory.ENVIRONMENT)
    )
    assert mode is PlanMode.PREDEFINED
    assert all(node.reused_from is None for node in plan.nodes)


def test_version_mismatch_invalidates_cache(env_platform, cache_entry):
    ctx = env_platform.world.context
    ctx.cached_checks = {
        "environment_state": cache_entry(Observation.ANOMALY, version="V100R001C00")
    }
    plan, mode = env_platform.planner.build_from_template(
        ctx, template_for(FailureCategory.ENVIRONMENT)
    )
    assert mode is PlanMode.PREDEFINED
    assert plan.nodes[0].reused_from is None


# ======================================================================
# 模式 C：动态生成
# ======================================================================
def test_dynamic_plan_targets_the_leader(env_platform):
    manager = _manager(env_platform)
    plan = env_platform.planner.build_dynamic(env_platform.world.context, manager)
    assert plan is not None
    assert plan.mode is PlanMode.DYNAMIC
    assert plan.nodes[0].skill == "check_environment_state"


def test_dynamic_plan_returns_none_when_nothing_is_informative(env_platform):
    """所有动作都跑过之后，规划器不该硬编一个「再查一遍」。"""
    manager = _manager(env_platform)
    executed = {spec.name for spec in env_platform.pool.diagnostic()}
    assert env_platform.planner.build_dynamic(
        env_platform.world.context, manager, executed=executed
    ) is None


def test_branching_avoids_parallel_resource_conflicts(make_platform):
    platform = make_platform("env", branching=True)
    manager = _manager(platform)
    plan = platform.planner.build_dynamic(platform.world.context, manager)
    assert plan is not None
    resources = []
    for node in plan.nodes:
        spec = platform.pool.require(node.skill)
        if spec.resource:
            resources.append(spec.resource)
    assert len(resources) == len(set(resources)), "同一批并行节点不能争抢同一资源"


def test_mode_selection_prefers_templates_only_on_first_round(env_platform):
    mode, template = env_platform.planner.select_mode(
        env_platform.world.context, FailureCategory.ENVIRONMENT, round_index=0
    )
    assert mode is PlanMode.PREDEFINED and template is not None
    mode2, template2 = env_platform.planner.select_mode(
        env_platform.world.context, FailureCategory.ENVIRONMENT, round_index=1
    )
    assert mode2 is PlanMode.DYNAMIC and template2 is None


# ======================================================================
# 校验器：八项检查
# ======================================================================
@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("cyclic", "CYCLE_DETECTED"),
        ("unauthorized", "UNAUTHORIZED_RECOVERY"),
        ("unknown_skill", "UNKNOWN_SKILL"),
        ("schema_mismatch", "SCHEMA_MISMATCH"),
        ("resource_conflict", "RESOURCE_CONFLICT"),
        ("budget", "BUDGET_NODE_COUNT"),
    ],
)
def test_validator_blocks_illegal_llm_plans(env_platform, style, expected):
    manager = _manager(env_platform)
    plan = _llm_plan(env_platform, style, manager)
    report = env_platform.validator.validate(
        plan, env_platform.world.context, budget=env_platform.config.budget
    )
    assert not report.ok
    assert expected in {issue.code for issue in report.blocking}


def test_validator_accepts_legal_llm_plan(env_platform):
    manager = _manager(env_platform)
    plan = _llm_plan(env_platform, "valid", manager)
    report = env_platform.validator.validate(
        plan, env_platform.world.context, budget=env_platform.config.budget
    )
    assert report.ok


def test_validator_rejects_empty_plan(env_platform):
    report = env_platform.validator.validate(DiagnosticPlan(nodes=[]))
    assert "EMPTY_PLAN" in {issue.code for issue in report.blocking}


def test_validator_detects_duplicate_node_ids(env_platform):
    plan = DiagnosticPlan(
        nodes=[
            PlanNode(id="N1", skill="inspect_config"),
            PlanNode(id="N1", skill="analyze_logs"),
        ]
    )
    report = env_platform.validator.validate(plan)
    assert "DUPLICATE_NODE_ID" in {issue.code for issue in report.blocking}


def test_validator_detects_missing_dependency(env_platform):
    plan = DiagnosticPlan(
        nodes=[PlanNode(id="N1", skill="inspect_config", depends_on=["N9"])]
    )
    report = env_platform.validator.validate(plan)
    assert "MISSING_DEPENDENCY" in {issue.code for issue in report.blocking}


def test_validator_blocks_r3_without_approval(env_platform):
    plan = DiagnosticPlan(
        nodes=[PlanNode(id="R", skill="restart_device")], rationale="直接重启"
    )
    report = env_platform.validator.validate(plan, allow_recovery=True)
    assert "PERMISSION_DENIED" in {issue.code for issue in report.blocking}


def test_validator_blocks_r4_even_with_recovery_allowed(env_platform):
    plan = DiagnosticPlan(nodes=[PlanNode(id="X", skill="override_test_expectation")])
    codes = {
        issue.code
        for issue in env_platform.validator.validate(plan, allow_recovery=True).blocking
    }
    assert "FORBIDDEN_RECOVERY" in codes


def test_sanitize_drops_bad_nodes_and_their_dependents(env_platform):
    plan = DiagnosticPlan(
        nodes=[
            PlanNode(id="N1", skill="inspect_config"),
            PlanNode(id="N2", skill="shell_exec"),
            PlanNode(id="N3", skill="analyze_logs", depends_on=["N2"]),
        ]
    )
    cleaned, report = env_platform.validator.sanitize(plan)
    assert not report.ok
    assert [node.id for node in cleaned.nodes] == ["N1"]


def test_sanitize_empties_plan_on_global_issue(env_platform):
    plan = DiagnosticPlan(
        nodes=[PlanNode(id=f"N{i}", skill="inspect_config") for i in range(1, 12)]
    )
    cleaned, report = env_platform.validator.sanitize(
        plan, budget=env_platform.config.budget
    )
    assert cleaned.nodes == []
    assert "BUDGET_NODE_COUNT" in {issue.code for issue in report.blocking}


# ======================================================================
# 恢复动作选择：唯一真相来源是 RecoveryController（不在规划器里）
# ======================================================================
def test_planner_does_not_own_recovery_selection(env_platform):
    """恢复动作的选取属于授权问题，只应由 RecoveryController 决定。"""
    assert not hasattr(env_platform.planner, "select_recovery")


def test_recovery_requires_objective_evidence_first(env_platform):
    """§7.2 第一道门：没有客观证据就不动世界。"""
    manager = _manager(env_platform)
    decision = env_platform.recovery.evaluate(manager)
    assert decision.action is None
    assert decision.blocked_reason == "NO_EVIDENCE"


def test_recovery_ladder_prefers_low_risk_tier(env_platform):
    manager = _manager(env_platform)
    manager.update(
        env_platform.pool.require("check_environment_state"), Observation.ANOMALY, ["EV_1"]
    )
    decision = env_platform.recovery.evaluate(manager)
    assert decision.action == "refresh_environment_state"
    assert decision.tier.value == "R1"
    assert decision.approved is True       # R1 可自动执行


def test_recovery_ladder_for_software_defect_is_defect_report(env_platform):
    manager = HypothesisManager.from_keys({"H_SOFTWARE": 1.0})
    # 软件缺陷没有客观支持证据时，验证门第一道就挡住了
    blocked = env_platform.recovery.evaluate(manager)
    assert blocked.action is None
    assert blocked.blocked_reason == "NO_EVIDENCE"

    manager.get("H_SOFTWARE").supporting_evidence = ["EV_1"]
    decision = env_platform.recovery.evaluate(manager)
    assert decision.action == "create_defect_report"
