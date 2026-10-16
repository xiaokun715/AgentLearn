"""编排、校验、执行与保存闭环的测试（说明书 §8 / §9 / §10 / §12）。"""
from __future__ import annotations

import pytest

from app.domain.enums import ExecutionMode, RouteTarget, StepStatus, TemplateFate, TemplateState
from app.domain.models import RunRequest, WorkflowStep, WorkflowTemplate
from app.domain.policy import SAVE_POLICY
from app.workflow.composer import CapabilityPlanner, ComposeError
from app.workflow.runtime import RuntimeOptions, WorkflowRuntime
from app.workflow.validator import WorkflowValidator

from .conftest import TREND_QUERY, TROUBLE_QUERY


# ======================================================================
# §8 生成
# ======================================================================
def test_planner_maps_each_capability_to_one_agent(platform):
    intent = platform.recognizer.recognize(TREND_QUERY)
    template = platform.planner.compose(intent)
    agent_caps = {platform.pool.require(s.agent).capabilities[0] for s in template.steps}
    assert set(intent.required_capabilities) <= agent_caps


def test_planner_is_deterministic(platform):
    """同一个意图必须生成同一份 DAG —— 否则「最佳编排」永远无法沉淀。"""
    intent = platform.recognizer.recognize(TREND_QUERY)
    first = platform.planner.compose(intent)
    second = platform.planner.compose(intent)
    assert first.structure_hash == second.structure_hash


def test_planner_orders_steps_by_data_dependency(platform):
    intent = platform.recognizer.recognize(TREND_QUERY)
    template = platform.planner.compose(intent)
    by_id = template.step_map()
    for step in template.steps:
        for key in step.needs:
            producers = [s for s in template.steps if key in s.produces]
            assert producers, f"{key} 没有人产出"
            assert any(producer.id in step.depends_on or producer.id == step.id
                       for producer in producers), f"{step.id} 没有依赖 {key} 的产出者"
    assert by_id  # 非空


def test_planner_raises_when_capability_has_no_provider(make_platform):
    """池子里没人提供这项能力时，生成必须**明确失败**，而不是少做一步。"""
    from app.domain.enums import IntentStatus
    from app.domain.models import Intent

    platform = make_platform()
    intent = Intent(name="ghost", status=IntentStatus.RESOLVED, confidence=0.9,
                    required_capabilities=["nonexistent_capability"], estimated_steps=1)
    with pytest.raises(ComposeError):
        platform.planner.compose(intent)


def test_planner_inserts_missing_producer(platform):
    """数据流不闭合时，规划器应该**补**一个生产者，而不是直接失败。"""
    planner = CapabilityPlanner(platform.pool)
    # 直接向 _satisfy_reads 传一个「要读 logs 但没人产出」的链
    chosen, inserted = planner._satisfy_reads(["log_analyzer"], [])
    assert "log_collector" in inserted
    assert chosen.index("log_collector") < chosen.index("log_analyzer")


# ======================================================================
# §9 校验
# ======================================================================
def _two_step(platform, *, dep: bool = True) -> WorkflowTemplate:
    return WorkflowTemplate(
        name="t",
        steps=[
            WorkflowStep(id="a", agent="metric_query", produces=["metrics"]),
            WorkflowStep(id="b", agent="trend_analyzer", depends_on=["a"] if dep else [],
                         needs=["metrics"], produces=["trend"]),
        ],
        tags=platform.store.all()[0].tags.model_copy(update={"intent": "data_trend"}),
    )


def test_validator_accepts_a_well_formed_dag(platform):
    validator = WorkflowValidator(platform.pool)
    assert validator.validate(_two_step(platform)).ok


def test_validator_detects_unordered_data_flow(platform):
    """「数据流闭合」与「数据流有序」是两件事。

    键有人产出、但产出者不在依赖链上 —— 这份 DAG 只是**碰巧**按 id 顺序
    能跑通，一旦并行执行就会读到空数据。
    """
    validator = WorkflowValidator(platform.pool)
    report = validator.validate(_two_step(platform, dep=False))
    assert "DATA_FLOW_UNORDERED" in {i.code for i in report.blocking}


def test_validator_detects_unclosed_data_flow(platform):
    validator = WorkflowValidator(platform.pool)
    template = WorkflowTemplate(
        name="t",
        steps=[WorkflowStep(id="a", agent="trend_analyzer", needs=["metrics"],
                            produces=["trend"])],
        tags=platform.store.all()[0].tags,
    )
    codes = {i.code for i in validator.validate(template).blocking}
    assert "DATA_FLOW_BROKEN" in codes


def test_validator_detects_cycle(platform):
    validator = WorkflowValidator(platform.pool)
    template = WorkflowTemplate(
        name="t",
        steps=[
            WorkflowStep(id="a", agent="metric_query", depends_on=["b"], produces=["metrics"]),
            WorkflowStep(id="b", agent="trend_analyzer", depends_on=["a"],
                         needs=["metrics"], produces=["trend"]),
        ],
        tags=platform.store.all()[0].tags,
    )
    assert "CYCLE" in {i.code for i in validator.validate(template).blocking}


def test_validator_flags_missing_capability(platform):
    validator = WorkflowValidator(platform.pool)
    intent = platform.recognizer.recognize(TROUBLE_QUERY)
    one_step = WorkflowTemplate(
        name="t",
        steps=[WorkflowStep(id="a", agent="log_collector", produces=["logs"])],
        tags=platform.store.all()[0].tags,
    )
    codes = {i.code for i in validator.validate(one_step, intent=intent).blocking}
    assert "CAPABILITY_MISSING" in codes


def test_validator_flags_budget_and_risk(platform):
    validator = WorkflowValidator(platform.pool)
    request = RunRequest(query=TREND_QUERY, max_steps=1, max_risk=__import__(
        "app.domain.enums", fromlist=["RiskLevel"]).RiskLevel.LOW)
    template = WorkflowTemplate(
        name="t",
        steps=[
            WorkflowStep(id="a", agent="config_inspector", produces=["config_diff"]),
            WorkflowStep(id="b", agent="config_applier", depends_on=["a"],
                         needs=["config_diff"], produces=["config_applied"]),
        ],
        tags=platform.store.all()[0].tags,
    )
    codes = {i.code for i in validator.validate(template, request=request).blocking}
    assert "BUDGET_STEPS" in codes
    assert "SIDE_EFFECT_STEP" in codes
    assert "RISK_EXCEEDS_BUDGET" in codes


def test_repair_drops_dangling_dependencies(platform):
    validator = WorkflowValidator(platform.pool)
    template = _two_step(platform)
    template.steps[1].depends_on = ["a", "ghost_step"]
    repaired, report = validator.repair(template)
    assert repaired.steps[1].depends_on == ["a"]
    assert report.repairs
    # repair **不能**改到调用方手上的那份 —— 调用方常常拿它和修好的版本做对比
    assert template.steps[1].depends_on == ["a", "ghost_step"]


def test_repair_reorders_to_break_a_false_cycle(platform):
    """有环但数据流本身成立时，按 reads/writes 重排应该能解开。"""
    validator = WorkflowValidator(platform.pool)
    template = WorkflowTemplate(
        name="t",
        steps=[
            WorkflowStep(id="a", agent="metric_query", depends_on=["b"], produces=["metrics"]),
            WorkflowStep(id="b", agent="trend_analyzer", depends_on=["a"],
                         needs=["metrics"], produces=["trend"]),
        ],
        tags=platform.store.all()[0].tags,
    )
    repaired, report = validator.repair(template)
    assert any("重新排序" in item for item in report.repairs)
    assert validator.validate(repaired).ok


# ======================================================================
# §10 执行
# ======================================================================
def test_runtime_executes_in_dependency_order(platform):
    runtime = WorkflowRuntime(platform.pool)
    intent = platform.recognizer.recognize(TREND_QUERY)
    template = platform.planner.compose(intent)
    trace = runtime.execute(template, intent)
    assert trace.ok
    assert "metrics" in trace.context and "trend" in trace.context


def test_runtime_total_time_is_critical_path_not_sum(platform):
    """编排的意义是并行 —— 求和会把并行的收益算没。"""
    runtime = WorkflowRuntime(platform.pool)
    intent = platform.recognizer.recognize(TROUBLE_QUERY)
    template = platform.store.by_intent("troubleshoot_failure")[0]
    trace = runtime.execute(template, intent)
    total_sum = sum(platform.pool.latency_of(s.agent) for s in template.steps)
    assert trace.total_ms < total_sum


def test_required_step_failure_blocks_downstream(platform):
    runtime = WorkflowRuntime(platform.pool)
    intent = platform.recognizer.recognize(TROUBLE_QUERY)
    template = platform.store.by_intent("troubleshoot_failure")[0]
    trace = runtime.execute(template, intent, RuntimeOptions(fail_steps={"s2": 99}))
    assert not trace.ok
    assert any(item.status is StepStatus.BLOCKED for item in trace.results)


def test_optional_step_failure_does_not_block_downstream(platform):
    """optional 的语义就是「不做也能继续」。"""
    runtime = WorkflowRuntime(platform.pool)
    intent = platform.recognizer.recognize(TROUBLE_QUERY)
    template = platform.store.by_intent("troubleshoot_failure")[0].model_copy(deep=True)
    for step in template.steps:
        if step.id == "s4":
            step.optional = True
    trace = runtime.execute(template, intent, RuntimeOptions(fail_steps={"s4": 99}))
    assert trace.ok
    assert any(item.status is StepStatus.SKIPPED for item in trace.results)


def test_step_retry_succeeds_on_second_attempt(platform):
    runtime = WorkflowRuntime(platform.pool)
    intent = platform.recognizer.recognize(TREND_QUERY)
    template = platform.planner.compose(intent)
    trace = runtime.execute(template, intent,
                            RuntimeOptions(max_retries=2, fail_steps={"g1": 1}))
    first = trace.results[0]
    assert first.status is StepStatus.SUCCEEDED
    assert first.attempts == 2


# ======================================================================
# §12 保存闭环
# ======================================================================
def test_forced_workflow_produces_a_pending_template(platform):
    result = platform.run(RunRequest(query=TREND_QUERY, actor="alice",
                                     mode=ExecutionMode.FORCE_WORKFLOW))
    assert result.decision.target is RouteTarget.COMPOSE_NEW
    assert result.template is not None
    assert result.template_fate is TemplateFate.PENDING
    assert result.compose_ms > 0
    assert result.total_ms > result.compose_ms


def test_direct_and_reuse_produce_no_template_to_decide(platform):
    direct = platform.run(RunRequest(query="总结一下上面的内容"))
    assert direct.template is None and direct.template_fate is TemplateFate.NONE
    reused = platform.run(RunRequest(query=TROUBLE_QUERY))
    assert reused.template_fate is TemplateFate.NONE


def test_save_attaches_tags_and_lands_in_library(platform):
    result = platform.run(RunRequest(query=TREND_QUERY, actor="alice",
                                     mode=ExecutionMode.FORCE_WORKFLOW))
    outcome = platform.decide_fate(result, TemplateFate.SAVE, actor="alice",
                                   labels=["容量看板"])
    assert outcome.stored
    stored = platform.store.get(outcome.template.template_id)
    assert stored.tags.author == "alice"
    assert stored.tags.created_at is not None
    assert "容量看板" in stored.tags.labels
    assert stored.tags.state is TemplateState.ACTIVE


def test_discard_marks_template_and_keeps_it_out_of_library(platform):
    result = platform.run(RunRequest(query=TREND_QUERY, actor="alice",
                                     mode=ExecutionMode.FORCE_WORKFLOW))
    before = len(platform.store.all())
    platform.decide_fate(result, TemplateFate.DISCARD)
    assert len(platform.store.all()) == before
    assert result.template.tags.state is TemplateState.BROKEN


def test_second_identical_intent_reuses_the_saved_template(platform):
    first = platform.run(RunRequest(query=TREND_QUERY, actor="alice",
                                    mode=ExecutionMode.FORCE_WORKFLOW))
    platform.decide_fate(first, TemplateFate.SAVE)
    second = platform.run(RunRequest(query=TREND_QUERY, actor="alice"))
    assert second.decision.target is RouteTarget.EXISTING_TEMPLATE
    assert second.compose_ms == 0
    assert second.decision.chosen == first.template.template_id


def test_templates_are_only_saved_after_a_successful_run(platform):
    result = platform.run(RunRequest(query=TREND_QUERY, actor="alice",
                                     mode=ExecutionMode.FORCE_WORKFLOW))
    assert result.template.stats.successes >= 1
    assert SAVE_POLICY.require_verified is True


def test_duplicate_generated_template_is_merged(platform):
    first = platform.run(RunRequest(query=TREND_QUERY, actor="alice",
                                    mode=ExecutionMode.FORCE_WORKFLOW))
    platform.decide_fate(first, TemplateFate.SAVE, labels=["A"])
    second = platform.run(RunRequest(query=TREND_QUERY, actor="bob",
                                     mode=ExecutionMode.FORCE_WORKFLOW))
    outcome = platform.decide_fate(second, TemplateFate.SAVE, labels=["B"])
    assert not outcome.stored and outcome.decision == "REJECT_DUPLICATE"
    merged = platform.store.get(outcome.merged_into)
    assert "alice" in merged.tags.author and "bob" in merged.tags.author


def test_reuse_updates_template_statistics(platform):
    first = platform.run(RunRequest(query=TREND_QUERY, actor="alice",
                                    mode=ExecutionMode.FORCE_WORKFLOW))
    platform.decide_fate(first, TemplateFate.SAVE)
    template = platform.store.get(first.template.template_id)
    runs_before = template.stats.runs
    platform.run(RunRequest(query=TREND_QUERY, actor="alice"))
    assert template.stats.runs == runs_before + 1


def test_high_risk_intent_cannot_be_auto_composed(platform):
    result = platform.run(RunRequest(query="修正配置并写回环境", actor="ops"))
    assert result.template is None
    assert result.template_fate is TemplateFate.NONE
    assert "SIDE_EFFECT_STEP" in " ".join(result.notes)


# ======================================================================
# 端到端
# ======================================================================
def test_runs_are_deterministic(platform):
    first = platform.run(RunRequest(query=TREND_QUERY, actor="alice",
                                    mode=ExecutionMode.FORCE_WORKFLOW))
    second = platform.run(RunRequest(query=TREND_QUERY, actor="alice",
                                     mode=ExecutionMode.FORCE_WORKFLOW))
    assert first.template.structure_hash == second.template.structure_hash
    assert first.total_ms == second.total_ms


def test_capability_matching_does_not_depend_on_agent_names(platform):
    """§4.2：路由与编排只看能力标签，不看 Agent 名字。

    做法是**换一个名字**把一个提供同样能力的 Agent 注册进池子，
    然后看编排器会不会用它 —— 编排逻辑里没有任何一处提到具体名字。
    """
    spec = platform.pool.require("log_collector")
    renamed = spec.model_copy(update={"name": "collector_v2"})
    platform.pool.register(renamed)
    platform.pool._agents.pop("log_collector")   # noqa: SLF001 - 测试专用

    # 能力查询能找到新名字
    providers = [item.name for item in platform.pool.by_capability("log_read")]
    assert providers == ["collector_v2"]

    # 现场编排会自动用上新名字，而不需要改任何一行编排代码
    intent = platform.recognizer.recognize(TROUBLE_QUERY)
    template = platform.planner.compose(intent)
    assert "collector_v2" in template.agents_used()
    assert platform.validator.validate(template).ok
