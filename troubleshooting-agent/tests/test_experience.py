"""故障经验库测试（说明书 §8）。

经验的正确性不在「存下来了」，而在三件事：
入库前是否真的过闸、检索后是否校验适用性、失败经验是否被保留且不被复用。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.enums import ExperienceStatus, Observation, TerminalState
from app.domain.models import (
    DiagnosisConclusion,
    Evidence,
    EvidenceDraft,
    ExperienceCase,
    FailureSignature,
)
from app.experience.manager import ExperienceManager
from app.sim import BASE_TIME


@pytest.fixture
def populated(env_platform):
    """跑一次完整排障，返回 (平台, 结论)。"""
    result = env_platform.troubleshoot(use_templates=False)
    return env_platform, result


# ======================================================================
# 指纹与适用性
# ======================================================================
def test_signature_matching_prefers_same_category():
    a = FailureSignature(failure_category="ENVIRONMENT", error_codes=["E1"],
                         software_version="V1")
    same = FailureSignature(failure_category="ENVIRONMENT", error_codes=["E1"],
                            software_version="V1")
    other = FailureSignature(failure_category="CONFIG", error_codes=["E1"],
                             software_version="V1")
    assert a.match_score(same) > a.match_score(other)


def test_signature_extracts_structured_log_patterns(env_platform):
    signature = env_platform.world.context.signature()
    assert "ERR_ENV_NOT_READY" in signature.log_patterns
    assert "ENV_NOT_READY" in signature.error_codes


def test_signature_does_not_include_raw_lines(env_platform):
    """整行日志带时间戳，直接当指纹会导致「同一故障永远检索不到自己」。"""
    signature = env_platform.world.context.signature()
    assert all("2026-10-15" not in pattern for pattern in signature.log_patterns)


def test_applicability_rejects_version_mismatch(env_platform):
    case = ExperienceCase(applicability={"software_versions": ["V100R020C10"]})
    ctx = env_platform.world.context.model_copy(update={"software_version": "V999"})
    applicable, reason = case.applicable_to(ctx)
    assert not applicable
    assert "版本" in reason


def test_failed_experience_is_never_applicable(env_platform):
    case = ExperienceCase(status=ExperienceStatus.FAILED)
    applicable, reason = case.applicable_to(env_platform.world.context)
    assert not applicable
    assert "失败经验" in reason


# ======================================================================
# 质量门控
# ======================================================================
def test_verified_case_is_published(env_platform, populated):
    platform, conclusion = populated
    case_id = conclusion.metrics["experience"]["case_id"]
    case = platform.experience.get(case_id)
    assert case is not None
    assert case.status is ExperienceStatus.VERIFIED
    assert case.verified_root_cause.key == "H_ENV"
    assert case.effective is True


def test_unresolved_diagnosis_produces_no_case(make_platform, tiny_budget):
    platform = make_platform("sparse", budget=tiny_budget)
    result = platform.troubleshoot(use_templates=False)
    assert result.terminal_state is TerminalState.UNRESOLVED
    assert result.metrics["experience"]["decision"] == "REJECT_UNRESOLVED"
    assert platform.experience.cases() == []


def test_incomplete_evidence_is_rejected(env_platform, populated):
    platform, conclusion = populated
    conclusion.evidence_ids = []          # 人为把证据抹掉
    conclusion.root_cause_key = "H_ENV"
    extraction = platform.experience.extract(conclusion, env_platform.world.context,
                                             platform.store)
    assert extraction.decision == "REJECT_INCOMPLETE_EVIDENCE"
    assert any("证据条数不足" in reason for reason in extraction.reasons)


def test_inferred_only_evidence_is_rejected(env_platform, populated):
    """全是推断、没有原始证据的经验不允许入库。"""
    platform, conclusion = populated
    inferred = platform.store.infer(
        source="llm", claim="可能是环境问题", derived_from=["EV_missing"]
    )
    conclusion.evidence_ids = [inferred.evidence_id, inferred.evidence_id]
    extraction = platform.experience.extract(conclusion, env_platform.world.context,
                                             platform.store)
    assert extraction.decision == "REJECT_INCOMPLETE_EVIDENCE"
    assert any("原始证据" in reason for reason in extraction.reasons)


def test_duplicate_case_is_rejected(env_platform, populated):
    """同指纹重复入库会被质量门挡回去（fixture 已经入过一条）。"""
    platform, conclusion = populated
    again = platform.experience.extract(
        conclusion, env_platform.world.context, platform.store
    )
    assert again.decision == "REJECT_DUPLICATE"
    assert any("指纹相同" in reason for reason in again.reasons)


def test_candidate_can_be_upgraded_to_verified(env_platform, populated):
    """同指纹的候选经验，在拿到验证结论后应升级而不是重复入库。"""
    platform, conclusion = populated
    fresh = ExperienceManager()          # 干净的库，避免被 fixture 里的已验证案例干扰

    candidate = conclusion.model_copy(deep=True)
    candidate.terminal_state = TerminalState.ROOT_CAUSE_IDENTIFIED
    candidate.recovery = []
    first = fresh.extract(candidate, env_platform.world.context, platform.store)
    assert first.case is not None and first.case.status is ExperienceStatus.CANDIDATE

    upgraded = fresh.extract(conclusion, env_platform.world.context, platform.store)
    assert upgraded.decision == "PUBLISH_UPGRADED"
    assert upgraded.case.status is ExperienceStatus.VERIFIED
    assert upgraded.case.effective is True


def test_failure_experience_is_recorded(env_platform, populated):
    platform, _ = populated
    failed = platform.experience.by_status(ExperienceStatus.FAILED)
    actions = [action for case in failed for action in case.recovery_actions]
    assert "refresh_environment_state" in actions


def test_repeated_failures_do_not_pile_up(env_platform):
    """失败经验按 (指纹, 动作集合) 去重 —— 否则同类故障每排一次就多一条噪声。"""
    ctx = env_platform.world.context
    first, created_1 = env_platform.experience.record_failure(
        ctx, attempted_actions=["refresh_environment_state"], symptom="没效果"
    )
    second, created_2 = env_platform.experience.record_failure(
        ctx, attempted_actions=["refresh_environment_state"], symptom="没效果"
    )
    assert created_1 is True
    assert created_2 is False
    assert first.case_id == second.case_id
    assert len(env_platform.experience.by_status(ExperienceStatus.FAILED)) == 1

    _, created_3 = env_platform.experience.record_failure(
        ctx, attempted_actions=["restart_device"], symptom="也没效果"
    )
    assert created_3 is True, "动作集合不同就该是新的一条"


def test_avoid_actions_surfaces_failed_paths(env_platform, populated):
    platform, _ = populated
    avoided = platform.experience.avoid_actions(env_platform.world.context)
    assert "refresh_environment_state" in avoided


# ======================================================================
# 检索与复用
# ======================================================================
def test_retrieval_returns_applicability_verdict(env_platform, populated):
    platform, _ = populated
    results = platform.experience.retrieve(env_platform.world.context)
    assert results
    assert all(isinstance(item.applicable, bool) for item in results)
    assert all(item.reason for item in results)


def test_seed_priors_only_uses_verified_applicable_cases(env_platform, populated):
    from app.hypothesis.manager import HypothesisManager

    platform, _ = populated
    manager = HypothesisManager.from_keys(
        {"H_ENV": 0.25, "H_CONFIG": 0.25, "H_TOOL": 0.25, "H_SOFTWARE": 0.25}
    )
    before = manager.get("H_ENV").prior
    retrievals = platform.experience.retrieve(env_platform.world.context)
    notes = platform.experience.seed_priors(manager, retrievals)
    assert manager.get("H_ENV").prior > before
    assert any("加权" in note for note in notes)
    # 先验归一化后仍然合法
    total = sum(h.posterior for h in manager.all())
    assert abs(total - 1.0) < 1e-9


def test_seed_priors_skips_failed_cases(env_platform, populated):
    from app.hypothesis.manager import HypothesisManager

    platform, _ = populated
    manager = HypothesisManager.from_keys({"H_ENV": 0.5, "H_CONFIG": 0.5})
    unchanged = manager.get("H_ENV").prior
    retrievals = [
        item for item in platform.experience.retrieve(env_platform.world.context)
        if item.case.status is ExperienceStatus.FAILED
    ]
    notes = platform.experience.seed_priors(manager, retrievals)
    assert manager.get("H_ENV").prior == unchanged
    assert any("跳过" in note for note in notes)


def test_experience_reuse_does_not_change_the_answer(make_platform):
    source = make_platform("env")
    first = source.troubleshoot(use_templates=False)
    assert first.root_cause_key == "H_ENV"

    reuse = make_platform("env")
    reuse.experience = source.experience
    second = reuse.build_agent().troubleshoot(
        reuse.world.context, use_templates=False
    )
    assert second.root_cause_key == "H_ENV"
    assert any("加权" in note for note in second.notes)


def test_disabling_experience_still_works(make_platform):
    source = make_platform("env")
    source.troubleshoot(use_templates=False)

    plain = make_platform("env")
    plain.experience = source.experience
    result = plain.build_agent().troubleshoot(
        plain.world.context, use_templates=False, use_experience=False
    )
    assert result.root_cause_key == "H_ENV"
    assert not any("经验检索" in note for note in result.notes)


def test_stats_counts_all_three_statuses(env_platform, populated):
    platform, _ = populated
    stats = platform.experience.stats()
    assert stats["total"] >= 2
    assert stats[ExperienceStatus.VERIFIED.value] >= 1
    assert stats[ExperienceStatus.FAILED.value] >= 1
    assert stats[ExperienceStatus.CANDIDATE.value] == 0


# ======================================================================
# 证据分层的结构性约束
# ======================================================================
def test_inferred_evidence_requires_provenance():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Evidence(kind="inferred", source="llm", claim="凭空推断")


def test_store_links_inferred_evidence_to_raw(env_platform):
    store = env_platform.store
    raw = store.record_raw(source="log", claim="原始日志", ref="artifact://x")
    inferred = store.infer(
        source="llm", claim="由此推断环境异常", derived_from=[raw.evidence_id]
    )
    assert raw.evidence_id in inferred.derived_from
    assert store.raw_ancestors([inferred.evidence_id]) == [raw.evidence_id]


def test_materialize_wires_hierarchy_automatically(env_platform):
    """工具只交草稿，溯源指针由证据库自己接上。"""
    drafts = [
        EvidenceDraft(kind="raw", claim="接口返回 404"),
        EvidenceDraft(kind="observed", claim="设备不可用"),
        EvidenceDraft(kind="inferred", claim="可能导致测试失败"),
    ]
    produced = env_platform.store.materialize(drafts, source="probe")
    kinds = [item.kind.value for item in produced]
    assert kinds == ["raw", "observed", "inferred"]
    inferred = produced[-1]
    assert inferred.derived_from, "推断必须指回上游证据"
    assert produced[0].evidence_id in env_platform.store.lineage(inferred.evidence_id)


def test_lineage_is_ordered_by_time(env_platform):
    store = env_platform.store
    raw = store.record_raw(source="a", claim="最早的原始证据")
    obs = store.record_observed(source="b", claim="观测", derived_from=[raw.evidence_id])
    lineage = store.lineage(obs.evidence_id)
    assert lineage[0] == raw.evidence_id


def test_snapshot_reports_evidence_mix(env_platform):
    env_platform.store.record_raw(source="a", claim="x")
    snapshot = env_platform.store.snapshot()
    assert snapshot["evidence_count"] == 1
    assert snapshot["kinds"]["raw"] == 1


def test_cache_entry_helper_matches_factory_expectations(env_platform, cache_entry):
    """T13 用的缓存条目必须能被模式 B 的校验器接受。"""
    from app.planning.planner import cache_valid

    ok, reason = cache_valid(cache_entry(Observation.ANOMALY), env_platform.world.context)
    assert ok, reason
    stale, stale_reason = cache_valid(
        cache_entry(Observation.ANOMALY, age_minutes=999), env_platform.world.context
    )
    assert not stale and "过期" in stale_reason


def test_experience_case_hash_is_stable():
    a = ExperienceCase(
        failure_signature=FailureSignature(failure_category="ENVIRONMENT",
                                           error_codes=["E", "D"]),
    )
    b = ExperienceCase(
        failure_signature=FailureSignature(failure_category="ENVIRONMENT",
                                           error_codes=["D", "E"]),
    )
    assert a.signature_hash == b.signature_hash


def test_base_time_fixture_sanity():
    """conftest 的缓存构造依赖这个基准时间。"""
    assert BASE_TIME.year == 2026
    assert (BASE_TIME + timedelta(minutes=5)).minute == 5


def test_unresolved_conclusion_has_no_root_cause(make_platform, tiny_budget):
    platform = make_platform("sparse", budget=tiny_budget)
    result = platform.troubleshoot(use_templates=False)
    assert isinstance(result, DiagnosisConclusion)
    assert result.root_cause is None
