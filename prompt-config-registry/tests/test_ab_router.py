"""A/B Router 测试：Hash 粘性、流量分布、Variant 命名（设计说明书 §15~§16）。"""
from __future__ import annotations

from app.domain.deployment import Deployment, DeploymentRule
from app.router.ab_router import AbRouter
from app.router.hash_router import bucket


def _dep(rules: list[DeploymentRule], experiment: str | None = None) -> Deployment:
    return Deployment(
        id="d1", agent_name="a", environment="prod", status="CANARY",
        rules=rules, experiment=experiment,
    )


def test_bucket_is_deterministic_and_sticky():
    """同一个 user 永远命中同一个 bucket（Sticky Assignment，§16）。"""
    for _ in range(50):
        assert bucket("user_123") == bucket("user_123")


def test_bucket_uniform_over_100k():
    """bucket 在 0~99 上近似均匀（用于灰度百分比精确生效）。"""
    bins = [0] * 100
    for i in range(100_000):
        bins[bucket(f"user_{i}")] += 1
    # 每个桶理论 1%；允许 ±0.3% 抖动
    assert all(0.007 <= b / 100_000 <= 0.013 for b in bins)


def test_single_version_routes_everyone():
    router = AbRouter()
    dep = _dep([DeploymentRule(version=12, weight=100)])
    for i in range(50):
        version, variant = router.route(dep, f"user_{i}")
        assert version == 12
        assert variant == "single"


def test_weighted_split_approximates_weights():
    """90/10 分流：统计占比应接近权重（2000 个用户样本）。"""
    router = AbRouter()
    dep = _dep(
        [DeploymentRule(version=12, weight=90), DeploymentRule(version=13, weight=10)],
        experiment="prompt_v13_test",
    )
    counts = {12: 0, 13: 0}
    for i in range(2_000):
        version, _ = router.route(dep, f"user_{i}")
        counts[version] += 1
    v13_fraction = counts[13] / 2_000
    assert 0.06 <= v13_fraction <= 0.14, counts


def test_salt_changes_grouping():
    """不同 experiment（salt）给同一批用户不同的分组 —— 实验互不干扰。"""
    dep_a = _dep([DeploymentRule(version=12, weight=50), DeploymentRule(version=13, weight=50)],
                 experiment="exp_a")
    dep_b = _dep([DeploymentRule(version=12, weight=50), DeploymentRule(version=13, weight=50)],
                 experiment="exp_b")
    router = AbRouter()
    group_a = [router.route(dep_a, f"user_{i}")[0] for i in range(20)]
    group_b = [router.route(dep_b, f"user_{i}")[0] for i in range(20)]
    # 20 个用户下两组完全相同的概率只有 (1/2)^20 —— 几乎不可能
    assert group_a != group_b


def test_variant_naming_control_first():
    """权重降序：A = 对照组（老版本 90%），B = 实验组（新版本 10%）。"""
    dep = _dep(
        [DeploymentRule(version=12, weight=90), DeploymentRule(version=13, weight=10)]
    )
    router = AbRouter()
    assert router.variant_for(dep, 12) == "A"
    assert router.variant_for(dep, 13) == "B"
