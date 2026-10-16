"""意图识别与路由的单元测试（说明书 §5 / §6 / §7）。"""
from __future__ import annotations

import pytest

from app.domain.enums import ExecutionMode, IntentStatus, RouteTarget
from app.domain.models import Intent, RunRequest
from app.domain.policy import ROUTING, smoothed_success
from app.routing.engine import RoutingEngine

from .conftest import REVIEW_QUERY, TREND_QUERY, TROUBLE_QUERY


# ======================================================================
# §5 意图识别
# ======================================================================
def test_rule_hit_wins(platform):
    intent = platform.recognizer.recognize(TROUBLE_QUERY)
    assert intent.decided_by == "rule"
    assert intent.name == "troubleshoot_failure"
    assert intent.status is IntentStatus.RESOLVED
    assert intent.confidence > 0.4


def test_estimated_steps_matches_capability_count(platform):
    """步数是**结构性**的：覆盖这些能力最少要几个 Agent。"""
    for query in [TROUBLE_QUERY, REVIEW_QUERY, TREND_QUERY]:
        intent = platform.recognizer.recognize(query)
        assert intent.estimated_steps == len(set(intent.required_capabilities))


def test_needs_orchestration_is_derived_from_steps(platform):
    multi = platform.recognizer.recognize(TROUBLE_QUERY)
    single = platform.recognizer.recognize("总结一下上面的内容")
    assert multi.needs_orchestration is True
    assert single.needs_orchestration is False


def test_vague_query_becomes_ambiguous_not_a_guess(platform):
    intent = platform.recognizer.recognize("帮我处理一下")
    assert intent.status is IntentStatus.AMBIGUOUS
    assert intent.confidence < 0.4
    assert not intent.actionable


def test_unknown_query_without_llm_falls_back_to_unknown(make_platform):
    platform = make_platform()
    platform.recognizer.llm = None
    intent = platform.recognizer.recognize("今天天气不错")
    assert intent.status is IntentStatus.UNKNOWN
    assert intent.decided_by == "unknown"


def test_forced_intent_overrides_rules(platform):
    intent = platform.recognizer.recognize(TROUBLE_QUERY, forced="knowledge_lookup")
    assert intent.name == "knowledge_lookup"
    assert intent.decided_by == "forced"
    assert intent.confidence > 0.9


def test_entities_are_extracted(platform):
    intent = platform.recognizer.recognize('分析 "SRS 优化" 模块的失败原因')
    assert intent.entities.get("subject") == "SRS 优化"


def test_llm_confidence_is_discounted(platform):
    intent = platform.recognizer.recognize("帮我处理一下")
    assert intent.confidence <= 0.7


# ======================================================================
# §6 路由
# ======================================================================
def _route(platform, query: str, **kwargs):
    request = RunRequest(query=query, **kwargs)
    intent = platform.recognizer.recognize(query)
    return intent, platform.router.route(intent, request)


def test_multi_step_intent_prefers_template_over_partial_agent(platform):
    _, decision = _route(platform, TROUBLE_QUERY)
    assert decision.target is RouteTarget.EXISTING_TEMPLATE


def test_partial_coverage_agents_are_blocked(platform):
    _, decision = _route(platform, TROUBLE_QUERY)
    partial = [c for c in decision.candidates
               if c.kind is RouteTarget.DIRECT_AGENT and c.coverage < 1.0]
    assert partial and all(c.blocked_reason for c in partial)


def test_single_capability_intent_goes_direct(platform):
    _, decision = _route(platform, "总结一下上面的内容")
    assert decision.target is RouteTarget.DIRECT_AGENT
    assert decision.chosen == "summarizer"


def test_ambiguous_intent_routes_to_clarify(platform):
    _, decision = _route(platform, "帮我处理一下")
    assert decision.target is RouteTarget.CLARIFY
    assert decision.candidates == []


def test_no_viable_candidate_becomes_compose_not_failure(platform):
    """没有候选**不是**失败，是「需要现场编排」。

    用 ``data_trend``：它需要 3 项能力、池子里没有单 Agent 能覆盖、
    也没有种子模板 —— 三条路里只剩「现场编排」。
    """
    assert platform.store.by_intent("data_trend") == [], "前提：没有对应的种子模板"
    _, decision = _route(platform, TREND_QUERY)
    assert decision.target is RouteTarget.COMPOSE_NEW
    assert decision.candidates, "候选会被列出并标注为什么不可行"


def test_decision_carries_full_candidate_list(platform):
    _, decision = _route(platform, TROUBLE_QUERY)
    assert len(decision.candidates) > 1
    assert all(hasattr(c, "coverage") for c in decision.candidates)


def test_coverage_weight_dominates_intent_weight():
    assert ROUTING.w_coverage > ROUTING.w_intent


def test_force_direct_picks_best_coverage_agent(platform):
    _, decision = _route(platform, TROUBLE_QUERY, mode=ExecutionMode.FORCE_DIRECT)
    assert decision.target is RouteTarget.DIRECT_AGENT
    assert decision.forced is True
    assert "覆盖不足" in decision.rationale or decision.score > 0


def test_force_workflow_skips_template_reuse(platform):
    """§7.1「强制」就是强制 —— 即使有完全匹配的模板也照样现场生成。"""
    assert platform.store.by_intent("troubleshoot_failure"), "前提：库里有匹配模板"
    _, decision = _route(platform, TROUBLE_QUERY, mode=ExecutionMode.FORCE_WORKFLOW)
    assert decision.target is RouteTarget.COMPOSE_NEW
    assert decision.forced is True
    assert decision.candidates == []


def test_offline_agent_removes_its_template_from_candidates(platform):
    from app.domain.enums import AgentHealth

    template = platform.store.by_intent("troubleshoot_failure")[0]
    platform.pool.set_health(template.agents_used()[0], AgentHealth.OFFLINE)
    platform.store.audit_health()
    _, decision = _route(platform, TROUBLE_QUERY)
    assert decision.chosen != template.template_id


# ======================================================================
# §6.3 平滑成功率
# ======================================================================
def test_smoothing_prevents_one_lucky_run_from_winning():
    assert smoothed_success(1, 1) < smoothed_success(39, 42)
    assert smoothed_success(1, 1) < 0.7


def test_smoothing_is_monotone_in_evidence():
    assert smoothed_success(90, 100) > smoothed_success(9, 10)


def test_smoothing_of_no_data_is_optimistic_but_not_trusting():
    assert smoothed_success(0, 0) == pytest.approx(0.5)


# ======================================================================
# §6.4 模板复用
# ======================================================================
def test_reuse_bonus_grows_logarithmically_and_caps():
    from app.domain.policy import reuse_bonus

    assert reuse_bonus(0, 20) == 0.0
    assert reuse_bonus(5, 20) < reuse_bonus(20, 20)
    assert reuse_bonus(1000, 20) == 1.0


def test_candidates_include_templates_for_matching_intent(platform):
    intent = Intent(
        name="troubleshoot_failure", status=IntentStatus.RESOLVED, confidence=0.9,
        required_capabilities=["log_read", "log_analysis", "env_check", "config_check",
                               "root_cause", "report"],
        estimated_steps=6,
    )
    engine = RoutingEngine(platform.pool, platform.store)
    decision = engine.route(intent, RunRequest(query=TROUBLE_QUERY))
    kinds = {c.kind for c in decision.candidates}
    assert RouteTarget.EXISTING_TEMPLATE in kinds


def test_template_with_wrong_intent_is_not_a_candidate(platform):
    intent = Intent(
        name="data_trend", status=IntentStatus.RESOLVED, confidence=0.9,
        required_capabilities=["metric_query", "trend_analysis", "chart"],
        estimated_steps=3,
    )
    engine = RoutingEngine(platform.pool, platform.store)
    decision = engine.route(intent, RunRequest(query=TREND_QUERY))
    template_candidates = [c for c in decision.candidates
                           if c.kind is RouteTarget.EXISTING_TEMPLATE]
    assert template_candidates == []
