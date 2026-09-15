"""反思与蒸馏：轨迹 → 可行流程/避坑教训/反思规律，以及 system.commit_session 落库。"""
from __future__ import annotations

from memory_engine.records import EPISODIC, SEMANTIC
from memory_engine.worker.distill import (
    DistilledFact,
    Step,
    distill_trajectory,
    reflect,
)


def test_successful_trajectory_distills_flow():
    facts = distill_trajectory(
        "排查 5G 基站闪断",
        [Step("查退服计数", ok=True), Step("看切换失败率", ok=True), Step("补邻区", ok=True)],
        outcome=True,
    )
    assert len(facts) == 1
    f: DistilledFact = facts[0]
    assert f.predicate == "可行流程"
    assert f.memory_type == EPISODIC
    assert f.confidence == 0.9
    assert "查退服计数" in f.object_value and "补邻区" in f.object_value


def test_failed_trajectory_distills_lesson_about_failing_step():
    facts = distill_trajectory(
        "排查 5G 基站闪断",
        [Step("查退服计数", ok=True), Step("直接重启小区", ok=False, detail="未定位根因")],
        outcome=False,
    )
    assert len(facts) == 1
    assert facts[0].predicate == "避坑教训"
    assert facts[0].confidence == 0.7
    assert "直接重启小区" in facts[0].object_value


def test_failure_without_failing_step_yields_nothing():
    # outcome=False 但没有任何 ok=False 的步骤 → 没有可提炼的失败点
    facts = distill_trajectory("目标", [Step("动作1", ok=True)], outcome=False)
    assert facts == []


def test_empty_steps_yield_nothing():
    assert distill_trajectory("目标", [], outcome=True) == []


def test_reflect_returns_semantic_rule_for_success_and_failure():
    ok_rule = reflect("排查 5G 基站闪断", outcome=True)
    fail_rule = reflect("排查 5G 基站闪断", outcome=False)
    for rule in (ok_rule, fail_rule):
        assert rule is not None
        assert rule.predicate == "反思规律"
        assert rule.memory_type == SEMANTIC
        assert rule.confidence == 0.6
    assert ok_rule.object_value != fail_rule.object_value


def test_step_dict_to_object_conversion():
    step = Step(**{"action": "抓 S1 信令", "ok": True, "detail": ""})
    assert step.action == "抓 S1 信令" and step.ok is True


# ---------------------------------------------------------------------
# system.commit_session 端到端
# ---------------------------------------------------------------------
def test_commit_success_writes_episodic_and_reflect_rule(rt_clean):
    saved = rt_clean.system.commit_session(
        "alice", "排查 VoLTE 掉话",
        [Step("查 EPS fallback 邻区", ok=True), Step("补 4G 邻区", ok=True)],
        outcome=True,
    )
    types = [r.memory_type for r in saved]
    assert EPISODIC in types and SEMANTIC in types
    assert rt_clean.engine.stats()["total"] == 2


def test_commit_failure_writes_only_lesson(rt_clean):
    saved = rt_clean.system.commit_session(
        "alice", "排查 5G 基站闪断",
        [Step("查退服计数", ok=True), Step("盲目重启", ok=False)],
        outcome=False,
    )
    assert len(saved) == 1
    assert saved[0].memory_type == EPISODIC
    assert saved[0].predicate == "避坑教训"


def test_commit_accepts_plain_dict_steps(rt_clean):
    saved = rt_clean.system.commit_session(
        "alice", "排查 5G 基站闪断",
        [{"action": "先看话统", "ok": True}],
        outcome=True,
    )
    assert len(saved) >= 1
