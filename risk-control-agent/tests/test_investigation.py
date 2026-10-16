"""动态调查的测试（说明书 §4 / §5）。"""
from __future__ import annotations

import pytest

from app.agent.investigator import Investigator
from app.agent.planner import CONTEXT_TOOLS, DISCRIMINATIVE_POWER
from app.domain.enums import HypothesisStatus
from app.domain.models import Hypothesis

from .conftest import CASES


# ======================================================================
# 规划器
# ======================================================================
def test_planner_skips_refuted_hypotheses(platform):
    """假设已经被反驳了还去查它，是纯浪费 —— 直接从候选里去掉，不是降分。"""
    hypotheses = {key: Hypothesis(key=key, description=key)
                  for key in ("H_BULK_REGISTER", "H_SHARED_DEVICE", "H_LEGITIMATE")}
    for item in hypotheses.values():
        item.weight = 0.33
    hypotheses["H_SHARED_DEVICE"].status = HypothesisStatus.REFUTED
    ranked = platform.planner.rank(hypotheses, called=set())
    for item in ranked:
        assert "H_SHARED_DEVICE" not in item.active_targets


def test_planner_requires_preconditions(platform):
    """图分析必须先有设备数据 —— 这不是优化，是正确性。"""
    hypotheses = {key: Hypothesis(key=key, description=key)
                  for key in ("H_SHARED_DEVICE", "H_BULK_REGISTER")}
    for item in hypotheses.values():
        item.weight = 0.5
    without = platform.planner.rank(hypotheses, called=set(), satisfied=set())
    assert "run_graph_analysis" not in [item.spec.name for item in without]
    with_device = platform.planner.rank(
        hypotheses, called={"query_device_graph"}, satisfied={"query_device_graph"})
    assert "run_graph_analysis" in [item.spec.name for item in with_device]


def test_planner_does_not_reuse_a_tool_that_returned_nothing(platform):
    """调用过但没拿到观测的工具不该被重选 —— 同一个查询不会有新结果。"""
    hypotheses = {key: Hypothesis(key=key, description=key)
                  for key in ("H_BULK_REGISTER", "H_LEGITIMATE")}
    for item in hypotheses.values():
        item.weight = 0.5
    ranked = platform.planner.rank(hypotheses, called={"query_account_profile"},
                                   satisfied=set())
    assert "query_account_profile" not in [item.spec.name for item in ranked]


def test_context_tools_are_only_a_fallback(platform):
    """上下文工具不区分假设 —— 有可做的检查时不该先去做案例检索。"""
    hypotheses = {key: Hypothesis(key=key, description=key)
                  for key in ("H_BULK_REGISTER", "H_LEGITIMATE")}
    for item in hypotheses.values():
        item.weight = 0.5
    hypotheses["H_LEGITIMATE"].status = HypothesisStatus.SUPPORTED      # 触发案例检索的条件
    ranked = platform.planner.rank(hypotheses, called=set())
    names = [item.spec.name for item in ranked]
    assert names and names[0] not in CONTEXT_TOOLS


def test_context_tools_do_not_declare_targets(platform):
    for name in CONTEXT_TOOLS:
        assert platform.tools[name].evidence_map == {}


def test_every_discriminating_tool_has_a_power_coefficient(platform):
    for spec in platform.tools.values():
        if spec.evidence_map:
            assert spec.name in DISCRIMINATIVE_POWER, spec.name


# ======================================================================
# 证据极性
# ======================================================================
def test_evidence_polarity_is_respected(platform, alert):
    """一个工具能区分假设，不等于它支持全部假设。

    早期版本只有一个 ``targets`` 列表，语义被简化成「ANOMALY 支持列表里的所有
    假设」—— 于是设备图查出多账号共用设备时，它同时「支持」了
    H_SHARED_DEVICE 和 H_LEGITIMATE。**一个工具报异常，不可能同时支持
    「这是团伙」和「这是正常用户」。**
    """
    spec = platform.tools["query_device_graph"]
    assert spec.evidence_map["H_SHARED_DEVICE"] == "+"
    assert spec.evidence_map["H_LEGITIMATE"] == "-"


def test_normal_observation_inverts_polarity(platform, alert):
    """设备图查出来正常，应当**反驳**「共享设备」而不是支持它。"""
    p = platform
    report, state = p.investigate(p.make_alert("u_normal_00", "x"))
    shared = state.hypotheses["H_SHARED_DEVICE"]
    assert shared.status is HypothesisStatus.REFUTED


# ======================================================================
# 结论门
# ======================================================================
def test_all_cases_reach_the_correct_conclusion(make_platform):
    for world, entity, truth in CASES:
        platform = make_platform(world)
        report, _ = platform.investigate(
            platform.make_alert(entity, "分歧", model_score=0.32))
        assert report.outcome.value == truth, f"{world}: {report.outcome.value} != {truth}"


def test_paths_differ_between_cases(make_platform):
    """**路径由证据决定，不是固定清单。**"""
    paths = {}
    for world, entity, _truth in CASES:
        platform = make_platform(world)
        report, _ = platform.investigate(
            platform.make_alert(entity, "分歧", model_score=0.32))
        paths[world] = tuple(report.investigation_path)
    assert len(set(paths.values())) > 1, paths


def test_unchecked_dimension_blocks_a_false_positive_verdict(make_platform):
    """「没查」不等于「没问题」—— 这是调查类系统最容易犯的假阴性。"""
    platform = make_platform("sparse_data")
    report, _ = platform.investigate(platform.make_alert("u_sparse_00", "x"))
    assert report.outcome.value == "INSUFFICIENT_EVIDENCE"
    assert report.missing_evidence
    assert any("H_SHARED_DEVICE" in item for item in report.missing_evidence)


def test_conflicting_evidence_does_not_auto_refute():
    """一条弱反证不该推翻三条强支持。"""
    item = Hypothesis(key="H_X", description="x")
    item.support = ["a", "b", "c"]
    item.refute = ["d"]

    class _Stub:
        hypotheses = {"H_X": item}

    Investigator._reweigh(_Stub())
    assert item.status is not HypothesisStatus.REFUTED


def test_pure_refutation_does_refute():
    item = Hypothesis(key="H_X", description="x")
    item.refute = ["d"]

    class _Stub:
        hypotheses = {"H_X": item}

    Investigator._reweigh(_Stub())
    assert item.status is HypothesisStatus.REFUTED


def test_inconclusive_updates_only_the_data_hypothesis(platform, alert):
    """「查不到」只支持「数据不足」，不能影响任何风险假设。"""
    p = platform
    _report, state = p.investigate(p.make_alert("u_sparse_00", "x"))
    assert state.hypotheses["H_INSUFFICIENT_DATA"].support


def test_budget_stops_the_loop(make_platform):
    """预算必须能截断调查 —— 否则一个数据源全挂的世界会跑遍所有工具。

    注意上限不能设得比「最短有效路径」还短，那会被启动自检拦下 ——
    这正是自检存在的意义：它挡的是「配置错了却没人发现」。
    """
    platform = make_platform("sparse_data", max_tool_calls=3)
    report, state = platform.investigate(platform.make_alert("u_sparse_00", "x"))
    assert report.tool_calls <= 3
    assert len(state.notes) > 0


def test_too_small_a_budget_is_rejected_at_startup(make_platform):
    with pytest.raises(RuntimeError, match="最短有效路径"):
        make_platform("sparse_data", max_tool_calls=1)


# ======================================================================
# 报告
# ======================================================================
def test_report_never_claims_execution(make_platform):
    for world, entity, _truth in CASES:
        platform = make_platform(world)
        report, _ = platform.investigate(platform.make_alert(entity, "x"))
        assert report.executed is False


def test_report_lists_evidence_and_path(make_platform):
    platform = make_platform("group_fraud")
    report, _ = platform.investigate(platform.make_alert("u_fraud_00", "x"))
    assert report.evidence_ids
    assert report.investigation_path
    assert report.to_report()


def test_hypothesis_weights_are_normalized(make_platform):
    platform = make_platform("group_fraud")
    _report, state = platform.investigate(platform.make_alert("u_fraud_00", "x"))
    total = sum(item.weight for item in state.hypotheses.values())
    assert total == pytest.approx(1.0, abs=0.02)


# ======================================================================
# 与固定工作流的对照（§5）
# ======================================================================
def test_fixed_workflow_runs_the_same_clist_every_time(make_platform):
    paths = []
    for world, entity, _truth in CASES:
        platform = make_platform(world)
        report, _ = platform.run_fixed_workflow(platform.make_alert(entity, "x"))
        paths.append(tuple(report.investigation_path))
    assert len(set(paths)) == 1, "固定流程对不同案件走同一条路径"


def test_fixed_workflow_is_wrong_when_data_is_missing(make_platform):
    """**本条是「为什么需要 Agent」的核心证据。**

    设备图不可用时，固定流程照样往下走，最后输出「未发现异常」。
    而正确结论应该是「证据不足」—— 它安静、可解释、看起来完全正常。
    """
    platform = make_platform("sparse_data")
    report, meta = platform.run_fixed_workflow(platform.make_alert("u_sparse_00", "x"))
    assert meta["inconclusive"] > 0
    assert report.outcome.value == "LIKELY_FALSE_POSITIVE"
    assert report.outcome.value != platform.world.truth["u_sparse_00"]


def test_fixed_workflow_cannot_report_missing_evidence(make_platform):
    platform = make_platform("sparse_data")
    report, _ = platform.run_fixed_workflow(platform.make_alert("u_sparse_00", "x"))
    assert report.missing_evidence == []
