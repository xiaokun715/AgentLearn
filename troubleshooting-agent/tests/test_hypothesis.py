"""假设管理与信息增益测试（说明书 §5）。

这里覆盖的是整个排障系统里**最容易被做错**的一段数学：
- 似然表缺失时按「无信息」处理，而不是当成强反驳；
- 熵、期望信息增益、实际信息增益三者口径一致；
- 结论门的三道硬门槛与「决定性检查」的例外。
"""
from __future__ import annotations

import math

import pytest

from app.domain.enums import FailureCategory, HypothesisStatus, Observation
from app.domain.models import FailureClassification
from app.domain.policy import CONCLUSION
from app.hypothesis.manager import HypothesisManager


@pytest.fixture
def manager() -> HypothesisManager:
    return HypothesisManager.from_classification(
        FailureClassification(primary_category=FailureCategory.ENVIRONMENT)
    )


# ======================================================================
# 构造
# ======================================================================
def test_from_classification_builds_multiple_hypotheses(manager):
    """§5.1「不要只生成一个根因」。"""
    assert len(manager.all()) >= 4
    assert manager.get("H_ENV") is not None


def test_priors_are_normalized(manager):
    assert math.isclose(sum(h.posterior for h in manager.all()), 1.0, abs_tol=1e-9)


def test_unknown_classification_uses_flat_priors():
    manager = HypothesisManager.from_classification(
        FailureClassification(primary_category=FailureCategory.UNKNOWN)
    )
    posteriors = [h.posterior for h in manager.all()]
    assert max(posteriors) - min(posteriors) < 0.06   # 近似均匀


# ======================================================================
# 熵与信息增益
# ======================================================================
def test_entropy_is_bounded_by_log2_of_hypothesis_count(manager):
    assert 0 < manager.entropy() <= math.log2(len(manager.all())) + 1e-9


def test_information_gain_of_uninformative_action_is_zero(env_platform, manager):
    """对所有假设似然相同的动作，信息增益恒为 0。"""
    spec = env_platform.pool.require("retrieve_failure_cases")
    assert env_platform.pool.get(spec.name) is spec
    assert manager.expected_information_gain(spec) == pytest.approx(0.0, abs=1e-12)


def test_targeted_check_has_higher_gain_than_broad_check(env_platform, manager):
    targeted = env_platform.pool.require("check_environment_state")
    broad = env_platform.pool.require("analyze_logs")
    assert manager.expected_information_gain(targeted) > manager.expected_information_gain(broad)


def test_single_hypothesis_has_no_uncertainty(env_platform):
    """只有一个候选时，熵为 0，任何检查的信息增益也只能是 0。"""
    manager = HypothesisManager.from_keys({"H_ENV": 1.0})
    assert manager.entropy() == pytest.approx(0.0)
    assert manager.expected_information_gain(
        env_platform.pool.require("check_environment_state")
    ) == 0.0


# ======================================================================
# 贝叶斯更新
# ======================================================================
def test_anomaly_raises_matching_hypothesis(env_platform, manager):
    spec = env_platform.pool.require("check_environment_state")
    before = manager.get("H_ENV").posterior
    report = manager.update(spec, Observation.ANOMALY, ["EV_1"])
    assert manager.get("H_ENV").posterior > before
    assert report.moved
    assert report.information_gain > 0


def test_normal_observation_refutes_matching_hypothesis(env_platform, manager):
    spec = env_platform.pool.require("check_environment_state")
    manager.update(spec, Observation.NORMAL, ["EV_1"])
    assert manager.get("H_ENV").posterior < manager.get("H_ENV").prior


def test_inconclusive_observation_changes_nothing(env_platform, manager):
    spec = env_platform.pool.require("check_environment_state")
    snapshot = {h.key: h.posterior for h in manager.all()}
    report = manager.update(spec, Observation.INCONCLUSIVE, [])
    assert report.moved == []
    assert {h.key: h.posterior for h in manager.all()} == snapshot
    assert report.information_gain == 0.0


def test_error_observation_changes_nothing(env_platform, manager):
    spec = env_platform.pool.require("analyze_logs")
    snapshot = {h.key: h.posterior for h in manager.all()}
    manager.update(spec, Observation.ERROR, ["EV_2"])
    assert {h.key: h.posterior for h in manager.all()} == snapshot


def test_missing_likelihood_row_is_treated_as_uninformative(env_platform, manager):
    """似然表缺项不能当成「不可能」，否则一次无关检查就能判死一个假设。"""
    spec = env_platform.pool.require("check_environment_state").model_copy(deep=True)
    spec.likelihoods = {"H_ENV": {"ANOMALY": 0.9, "NORMAL": 0.1}}
    before = manager.get("H_TOOL").posterior
    manager.update(spec, Observation.NORMAL, ["EV_3"])
    # H_TOOL 没有似然行 -> 权重不变；归一化后它只因为 H_ENV 变化而被动变化，
    # 但绝不该出现「被一次与它无关的检查直接判死」那种崩塌
    assert manager.get("H_TOOL").posterior > before * 0.5


def test_posteriors_stay_normalized_after_updates(env_platform, manager):
    for name, observation in (
        ("check_environment_state", Observation.NORMAL),
        ("inspect_config", Observation.ANOMALY),
        ("analyze_logs", Observation.ANOMALY),
    ):
        manager.update(env_platform.pool.require(name), observation, ["EV_x"])
    assert math.isclose(sum(h.posterior for h in manager.all()), 1.0, abs_tol=1e-9)


def test_evidence_is_attached_to_the_right_side(env_platform, manager):
    spec = env_platform.pool.require("check_environment_state")
    manager.update(spec, Observation.ANOMALY, ["EV_support"])
    manager.update(spec, Observation.NORMAL, ["EV_contradict"])
    assert "EV_support" in manager.get("H_ENV").supporting_evidence
    assert "EV_support" not in manager.get("H_ENV").contradicting_evidence


def test_small_moves_still_attach_evidence(env_platform, manager):
    """弱支持也要记账：证据挂载阈值（0.02）低于「有区分力」阈值（0.08）。"""
    assert CONCLUSION.evidence_attach_epsilon < CONCLUSION.posterior_move_epsilon
    spec = env_platform.pool.require("inspect_tool_records")
    manager.update(spec, Observation.ANOMALY, ["EV_small"])
    moved_keys = [h.key for h in manager.all() if h.supporting_evidence]
    assert moved_keys, "至少有一个假设应该收到弱支持证据"


# ======================================================================
# 结论门
# ======================================================================
def test_no_conclusion_before_any_check(manager):
    ready, reason = manager.conclusion_ready()
    assert not ready
    assert "后验" in reason


def test_no_conclusion_without_discriminating_check(env_platform, manager):
    """「没被反驳」不等于「被支持」—— 这是本 Demo 最重要的一条门。"""
    leader = manager.get("H_ENV")
    leader.posterior = 0.95
    leader.supporting_evidence = ["EV_a", "EV_b", "EV_c"]
    leader.discriminating_checks = []
    ready, reason = manager.conclusion_ready()
    assert not ready
    assert "主动推动" in reason


def test_conclusion_after_decisive_check(env_platform, manager):
    manager.update(
        env_platform.pool.require("check_environment_state"), Observation.ANOMALY, ["EV_1"]
    )
    ready, _ = manager.conclusion_ready()
    assert ready
    assert manager.max_move("H_ENV") >= CONCLUSION.decisive_move


def test_no_margin_gate_because_it_would_be_unreachable(env_platform, manager):
    """记录一条「被算掉的门」：归一化之后，边际门是不可能触发的。

    后验和为 1，因此「领跑 ≥ 0.75」必然蕴含「次席 ≤ 0.25」，也就是差距 ≥ 0.50。
    任何小于 0.50 的边际阈值都永远为真 —— 写上它只会让人误以为多了一层保护。
    这条测试把这件事钉住，免得以后有人又把它加回来。
    """
    manager.update(
        env_platform.pool.require("check_environment_state"), Observation.ANOMALY, ["EV_1"]
    )
    leader = manager.leader()
    runner = manager.runner_up()
    assert leader.posterior >= CONCLUSION.accept_probability
    if runner is not None:
        # leader + runner ≤ 1  =>  runner ≤ 0.25  =>  margin ≥ 0.50
        assert leader.posterior - runner.posterior >= 2 * CONCLUSION.accept_probability - 1
    ready, reason = manager.conclusion_ready()
    assert ready
    assert "领先次席" in reason


def test_accept_probability_forces_a_wide_margin(env_platform):
    """accept_probability 的副产品：次席最多只有 0.25。"""
    assert CONCLUSION.accept_probability > 0.5


def test_source_based_gate_requires_two_independent_sources(env_platform, manager):
    """按「独立来源数」计数，而不是按证据条数 —— 一次检查会产出多条证据。"""
    spec = env_platform.pool.require("check_environment_state")
    report = manager.update(spec, Observation.ANOMALY, ["EV_1", "EV_2", "EV_3"])
    assert report.moved
    # 手工把概率顶到阈值以上，排除其它三道门的干扰，只看来源数这一道
    manager.get("H_ENV").posterior = 0.90
    manager.get("H_TOOL").posterior = 0.02
    manager.renormalize()
    manager.get("H_ENV").posterior = 0.90
    manager.get("H_ENV").discriminating_checks = ["check_environment_state"]
    manager.get("H_ENV").supporting_evidence = ["EV_1", "EV_2", "EV_3"]
    source_of = lambda eid: "check_environment_state"   # noqa: E731 - 三条件同源
    manager.get("H_ENV").history = [{"delta": 0.01}]     # 去掉决定性豁免
    ready, reason = manager.conclusion_ready(source_of)
    assert not ready
    assert "独立来源" in reason


def test_finalize_marks_untested_hypotheses_undecided(manager):
    manager.finalize_statuses()
    assert all(h.status is HypothesisStatus.UNDECIDED for h in manager.all())


def test_refuted_requires_contradicting_evidence(env_platform, manager):
    """先验本来就低的假设，不该在没查之前就被标成「已排除」。"""
    low = manager.get("H_CASE")
    assert low.posterior < CONCLUSION.refute_probability
    assert low.status is not HypothesisStatus.REFUTED
    manager.update(
        env_platform.pool.require("check_environment_state"), Observation.ANOMALY, ["EV_1"]
    )
    assert manager.get("H_CASE").status is HypothesisStatus.REFUTED
    assert manager.get("H_CASE").contradicting_evidence


def test_prior_boost_only_moves_priors(manager):
    """轨迹线索只调先验，不计成证据（否则「可疑」就等于「证实」）。"""
    before_posterior = manager.get("H_ENV").posterior
    manager.boost_prior("H_ENV", 0.25, "轨迹线索")
    assert manager.get("H_ENV").posterior > before_posterior
    assert manager.get("H_ENV").supporting_evidence == []
    ready, _ = manager.conclusion_ready()
    assert not ready
