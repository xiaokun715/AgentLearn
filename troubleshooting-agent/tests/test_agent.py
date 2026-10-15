"""端到端测试：排障主循环（说明书 §9 / §10.3）。

这里断言的是**系统级不变量**，而不是某个函数的行为：
终止状态的语义、预算的约束力、轨迹与证据的职责边界、
以及「不同世界的根因都能被证据捞出来」。
"""
from __future__ import annotations

import pytest

from app.agent.agent import TroubleshootingAgent, describe
from app.domain.enums import FailureCategory, PlanMode, TerminalState
from app.domain.models import Budget, FailureContext


def test_invalid_input_short_circuits(make_platform):
    """缺必要输入时直接 INVALID_INPUT，不浪费预算「假装诊断」。"""
    platform = make_platform("env")
    broken = FailureContext(task_id="", test_case_id="")
    result = platform.build_agent().troubleshoot(broken)
    assert result.terminal_state is TerminalState.INVALID_INPUT
    assert "task_id" in (result.unresolved_reason or "")
    assert result.metrics["tool_calls"] == 0


def test_missing_expected_and_actual_is_invalid(make_platform):
    platform = make_platform("env")
    ctx = platform.world.context.model_copy(
        update={"expected_result": {}, "actual_result": {}}
    )
    result = platform.build_agent().troubleshoot(ctx)
    assert result.terminal_state is TerminalState.INVALID_INPUT


@pytest.mark.parametrize(
    ("world", "expected_key"),
    [("env", "H_ENV"), ("config", "H_CONFIG"), ("case", "H_CASE")],
)
def test_every_world_is_diagnosed(make_platform, world, expected_key):
    platform = make_platform(world)
    result = platform.troubleshoot(use_templates=False)
    assert result.root_cause_key == expected_key
    assert result.terminal_state.is_success


def test_sparse_world_stays_unresolved(make_platform, tiny_budget):
    platform = make_platform("sparse", budget=tiny_budget)
    result = platform.troubleshoot(use_templates=False)
    assert result.terminal_state is TerminalState.UNRESOLVED
    assert result.root_cause is None
    assert result.unresolved_reason
    assert result.metrics["steps"] <= tiny_budget.max_steps


def test_dynamic_mode_uses_fewer_checks_than_template(make_platform):
    dynamic = make_platform("env").troubleshoot(use_templates=False)
    template = make_platform("env").troubleshoot(use_templates=True)
    assert dynamic.root_cause_key == template.root_cause_key == "H_ENV"
    assert dynamic.metrics["distinct_diagnostic_actions"] < \
        template.metrics["distinct_diagnostic_actions"]
    assert template.metrics["plan_modes"] == [PlanMode.PREDEFINED.value]


def test_adaptive_loop_corrects_a_wrong_initial_classification(make_platform):
    """LLM 猜了 CONFIG，证据把它纠正成 CASE —— 这是整章最想展示的一件事。"""
    platform = make_platform("case")
    result = platform.troubleshoot(use_templates=False)
    assert result.classification.primary_category is FailureCategory.CONFIG
    assert result.classification.decided_by == "llm"
    assert result.root_cause_key == "H_CASE"
    # 诊断路径体现「一步一步缩小范围」，而不是一次跑完全部检查
    assert len(result.diagnostic_path) >= 3
    assert result.metrics["tool_calls"] == len(result.diagnostic_path)


def test_budget_stops_the_loop(make_platform):
    budget = Budget(max_steps=1, max_tool_calls=3, max_seconds=600, max_recovery_attempts=3)
    platform = make_platform("case", budget=budget)
    result = platform.troubleshoot(use_templates=False)
    assert result.terminal_state is TerminalState.UNRESOLVED
    assert result.metrics["steps"] <= 1
    assert "最大诊断步数" in (result.unresolved_reason or "")


def test_tool_call_budget_also_stops_the_loop(make_platform):
    budget = Budget(max_steps=99, max_tool_calls=1, max_seconds=600, max_recovery_attempts=3)
    platform = make_platform("case", budget=budget)
    result = platform.troubleshoot(use_templates=False)
    assert result.terminal_state is TerminalState.UNRESOLVED
    assert result.metrics["tool_calls"] == 1
    assert "max_tool_calls" in (result.unresolved_reason or "")


def test_no_check_is_executed_twice_in_dynamic_mode(make_platform):
    platform = make_platform("case")
    result = platform.troubleshoot(use_templates=False)
    diagnostics = [item for item in result.diagnostic_path if ":" not in item]
    assert len(diagnostics) == len(set(diagnostics))


def test_trace_hints_only_touch_priors(make_platform):
    """轨迹线索的产物必须是「先验变化」而不是「支持证据」。"""
    platform = make_platform("env")
    result = platform.troubleshoot(use_templates=False)
    assert any("轨迹线索" in note for note in result.notes)
    h_env = next(item for item in result.hypotheses if item["key"] == "H_ENV")
    assert h_env["status"] == "SUPPORTED"


def test_conclusion_evidence_is_traceable_to_raw(make_platform):
    platform = make_platform("env")
    result = platform.troubleshoot(use_templates=False)
    assert result.evidence_ids
    raw = platform.store.raw_ancestors(result.evidence_ids)
    assert raw
    assert all(platform.store.get(eid).kind.value in ("raw", "observed") for eid in raw)


def test_notes_record_every_round(make_platform):
    """每轮开始都过一次结论门；通过的那一次不再消耗工具调用，所以是 steps+1 条。"""
    platform = make_platform("case")
    result = platform.troubleshoot(use_templates=False)
    rounds = [note for note in result.notes if "轮结论门" in note]
    assert len(rounds) == result.metrics["steps"] + 1
    assert "后验" in rounds[-1]


def test_every_executed_action_is_explained_in_notes(make_platform):
    """每一步都要留下「为什么走这一步」的记录，否则复盘无从谈起。"""
    platform = make_platform("case")
    result = platform.troubleshoot(use_templates=False)
    for skill in result.diagnostic_path:
        if ":" in skill or skill == "rerun_test":
            continue
        assert any(skill in note for note in result.notes), skill


def test_llm_proposed_plan_is_validated_then_replaced(make_platform):
    """LLM 提议的非法计划会被拦下并回退到贪心 —— 循环照常收敛。"""
    platform = make_platform("env", plan_style="unauthorized", use_llm_planner=True)
    result = platform.troubleshoot(use_templates=False)
    assert any("未通过校验" in note for note in result.notes)
    assert "重启" not in result.diagnostic_path
    assert result.root_cause_key == "H_ENV"


def test_llm_proposed_valid_plan_is_adopted(make_platform):
    platform = make_platform("env", plan_style="valid", use_llm_planner=True)
    result = platform.troubleshoot(use_templates=False)
    assert any("采纳 LLM 提议的计划" in note for note in result.notes)


def test_report_is_human_readable(make_platform):
    platform = make_platform("env")
    result = platform.troubleshoot(use_templates=False)
    report = result.to_report()
    assert "终止状态" in report and "根因" in report


def test_describe_summarises_terminal_state(make_platform):
    platform = make_platform("env")
    result = platform.troubleshoot(use_templates=False)
    assert describe(result).startswith("RESOLVED")


def test_repeated_runs_are_deterministic(make_platform):
    """同样的世界跑两次，诊断路径与结论必须完全一致（否则无从回归）。"""
    first = make_platform("case").troubleshoot(use_templates=False)
    second = make_platform("case").troubleshoot(use_templates=False)
    assert first.diagnostic_path == second.diagnostic_path
    assert first.root_cause_key == second.root_cause_key
    assert first.confidence == second.confidence
    assert first.metrics["tool_calls"] == second.metrics["tool_calls"]


def test_checkpoints_are_written_and_end_with_the_final_state(make_platform, tmp_path):
    """收尾也要落一次快照，否则进程挂掉时磁盘上只剩 terminal=None 的旧状态。"""
    platform = make_platform("env", checkpoint_dir=tmp_path / "ck")
    platform.build_agent().troubleshoot(platform.world.context, run_id="ck_run")
    saved = platform.executor.checkpoints.load("ck_run")
    assert saved is not None
    assert saved["phase"] == "final"
    assert saved["terminal"] == TerminalState.RESOLVED.value
    assert saved["root_cause_key"] == "H_ENV"
    assert (tmp_path / "ck" / "ck_run.json").exists()


def test_agent_can_be_constructed_with_overrides(make_platform):
    platform = make_platform("env")
    agent = platform.build_agent(use_llm_planner=True)
    assert isinstance(agent, TroubleshootingAgent)
    assert agent.use_llm_planner is True
