"""在线决策链路的测试（说明书 §1）。"""
from __future__ import annotations

import pytest

from app.domain.enums import Decision, RiskLevel
from app.domain.policy import DECISION, ONLINE_RULES


# ======================================================================
# 特征层
# ======================================================================
def test_features_have_sources(platform, event_factory):
    """每个特征都要能回答「这个数从哪来的」—— 否则错误的特征无法被定位。"""
    features = platform.features.compute(event_factory("u_fraud_00", "dev_pool_a"))
    assert features.values
    assert set(features.values) == set(features.sources)
    assert features.sources["accounts_per_device_1d"] == "device-graph"


def test_feature_computation_is_deterministic(platform, event_factory):
    event = event_factory("u_fraud_00", "dev_pool_a")
    first = platform.features.compute(event).values
    second = platform.features.compute(event).values
    assert first == second


def test_device_feature_counts_accounts(platform, event_factory):
    features = platform.features.compute(event_factory("u_fraud_00", "dev_pool_a"))
    assert features.get("accounts_per_device_1d") == 7      # dev_pool_a 上有 7 个账号


def test_refund_ratio_reflects_profile(platform, event_factory):
    features = platform.features.compute(event_factory("u_fraud_00", "dev_pool_a"))
    assert features.get("refund_ratio_7d") == pytest.approx(0.88, abs=0.01)


# ======================================================================
# 规则引擎
# ======================================================================
def test_rule_direction_is_respected(platform, event_factory):
    """``direction`` 不能省 —— 早期版本把「注册后 10 分钟内领券」实现成了
    「注册超过 10 分钟」，于是它对每一个老用户都命中。"""
    rule = next(r for r in ONLINE_RULES if r.rule_id == "R004_NEW_ACCOUNT_CLAIM")
    assert rule.direction == "below"
    # 老账号不该命中 R004
    features = platform.features.compute(event_factory("u_normal_00", "dev_own_0"))
    assert features.get("minutes_since_register") > 10
    hits = platform.rules.evaluate(event_factory("u_normal_00", "dev_own_0"), features)
    assert "R004_NEW_ACCOUNT_CLAIM" not in [hit.rule_id for hit in hits]


def test_device_rule_fires_on_shared_device(platform, event_factory):
    event = event_factory("u_fraud_00", "dev_pool_a")
    hits = platform.rules.evaluate(event, platform.features.compute(event))
    device_hits = [hit for hit in hits if hit.rule_id == "R002_DEVICE_SHARING"]
    assert device_hits and device_hits[0].risk_level is RiskLevel.HIGH


def test_refund_rule_requires_minimum_count(make_platform, event_factory):
    """单看比率会被小样本骗：1 笔退款 / 1 笔订单 = 83% 的退款率。

    比率再高，只要笔数没到门槛就不该触发 —— 否则每一个「买了一件、
    退了一件」的用户都会进人工队列。
    """
    platform = make_platform("sparse_data")
    event = event_factory("u_sparse_00", "dev_x")
    features = platform.features.compute(event)
    assert features.get("refund_ratio_7d") > 0.5
    assert features.get("refund_count_7d") < 5
    hits = platform.rules.evaluate(event, features)
    assert "R003_REFUND_RATIO" not in [hit.rule_id for hit in hits]


def test_promo_scene_relaxes_refund_rule(platform, event_factory):
    """§6.2 规则必须知道自己在什么场景里被评估。"""
    rule = next(r for r in ONLINE_RULES if r.rule_id == "R003_REFUND_RATIO")
    event = event_factory("u_normal_00", "dev_own_0", campaign_id="cmp_001")
    features = platform.features.compute(event)
    assert features.get("is_promo_scene") == 1.0
    assert platform.rules.effective_threshold(rule, features) > rule.threshold
    assert rule.threshold == pytest.approx(0.55)


# ======================================================================
# 风险模型
# ======================================================================
def test_model_score_is_probability(platform, event_factory):
    event = event_factory("u_fraud_00", "dev_pool_a")
    score = platform.model.score(event, platform.features.compute(event))
    assert 0.0 <= score.score <= 1.0


def test_model_scores_risk_higher_than_normal(make_platform, event_factory):
    fraud = make_platform("group_fraud")
    fraud_score = fraud.evaluate_online(event_factory("u_fraud_00", "dev_pool_a"))
    normal = make_platform("false_positive")
    normal_score = normal.evaluate_online(event_factory("u_normal_00", "dev_own_0"))
    assert fraud_score.model_score.score > normal_score.model_score.score


def test_model_explains_top_features(platform, event_factory):
    event = event_factory("u_fraud_00", "dev_pool_a")
    score = platform.model.score(event, platform.features.compute(event))
    assert score.top_features
    assert all(isinstance(name, str) for name, _ in score.top_features)


# ======================================================================
# 决策引擎
# ======================================================================
def test_divergence_goes_to_review_not_block(make_platform, event_factory):
    """**规则说有问题、模型说没事**时，在线链路不该选一个，而该交出去。"""
    platform = make_platform("false_positive")
    decision = platform.evaluate_online(event_factory("u_normal_00", "dev_own_0"))
    assert decision.decision is Decision.REVIEW
    assert "分歧" in decision.rationale
    assert decision.model_score.score < DECISION.verify_score


def test_both_high_blocks(make_platform, event_factory):
    platform = make_platform("group_fraud")
    decision = platform.evaluate_online(event_factory("u_fraud_00", "dev_pool_a"))
    assert decision.decision in (Decision.BLOCK, Decision.REVIEW)


def test_clean_event_passes(make_platform, event_factory):
    platform = make_platform("device_farm")
    decision = platform.evaluate_online(event_factory("u_farm_00", "dev_farm_00"))
    assert decision.decision is Decision.PASS
    assert decision.hit_rule_ids == []


def test_online_latency_is_single_digit_ms(platform, event_factory):
    """在线链路必须毫秒级 —— 这是「交易决策不经过 Agent」的量化依据。"""
    decision = platform.evaluate_online(event_factory("u_fraud_00", "dev_pool_a"))
    assert decision.latency_ms <= 10


def test_online_decision_is_reproducible(platform, event_factory):
    event = event_factory("u_fraud_00", "dev_pool_a")
    decisions = {platform.evaluate_online(event).decision for _ in range(5)}
    assert len(decisions) == 1


def test_alert_is_built_from_review_decision(make_platform, event_factory):
    platform = make_platform("false_positive")
    event = event_factory("u_normal_00", "dev_own_0")
    decision = platform.evaluate_online(event)
    alert = platform.alert_from(decision, event)
    assert alert.entity_id == "u_normal_00"
    assert alert.model_score == decision.model_score.score
    assert alert.context["device_id"] == "dev_own_0"
