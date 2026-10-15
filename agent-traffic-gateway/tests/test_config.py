"""配置与自检测试（说明书 §17 / §18）。

§18 的最后一条反模式是「阈值写死在代码里」；本文件测的是它的另一半：
**阈值集中之后，它们之间的自洽性有没有人管**。
"""
from __future__ import annotations

import pytest

from app.domain.enums import QueuePolicy, UpgradeSignal
from app.domain.models import GatewayConfig
from app.domain.policy import DEFAULT_UPSTREAM, governed, target_rps, ungoverned
from app.factory import check_config, config_to_yaml, load_config


# ======================================================================
# 结构
# ======================================================================
def test_config_is_immutable_by_copy():
    """``with_`` 必须返回新对象 —— 否则场景之间会互相污染。"""
    base = GatewayConfig()
    tweaked = base.with_(queue_capacity=8)
    assert base.queue_capacity != 8
    assert tweaked.queue_capacity == 8


def test_ungoverned_disables_every_guard():
    config = ungoverned()
    assert not config.admission_enabled
    assert not config.sweep_expired
    assert not config.coalescing_enabled
    assert not config.retry_enabled
    assert not config.shedding_enabled
    assert not config.degrade_enabled
    assert config.queue_capacity > 10_000
    assert config.upstream_concurrency > DEFAULT_UPSTREAM.max_concurrency


def test_shadowing_with_ungoverned_still_bounded():
    config = ungoverned().with_(queue_capacity=64, l1_capacity=2048)
    assert config.queue_capacity == 64
    assert config.l1_capacity == 2048
    assert not config.admission_enabled


# ======================================================================
# 自检
# ======================================================================
def test_default_governed_config_passes():
    report = check_config(governed(), DEFAULT_UPSTREAM)
    assert report.ok, report.problems


def test_concurrency_above_upstream_quota_is_flagged():
    report = check_config(governed(upstream_concurrency=64), DEFAULT_UPSTREAM)
    assert any("上游配额" in p for p in report.problems)


def test_edge_limit_below_capacity_is_flagged():
    """§6.2 L1 必须「略高于」总容量，否则会先于准入控制误伤正常流量。"""
    report = check_config(governed(edge_rps=1.0, edge_burst=2), DEFAULT_UPSTREAM)
    assert any("edge_rps" in p for p in report.problems)


def test_queue_smaller_than_concurrency_is_flagged():
    report = check_config(governed(queue_capacity=2))
    assert any("queue_capacity" in p for p in report.problems)


def test_useless_degradation_is_flagged():
    report = check_config(governed(degrade_enabled=True, degrade_service_factor=1.2))
    assert any("degrade_service_factor" in p for p in report.problems)


def test_incoherent_shedding_thresholds_are_flagged():
    """§7.4：丢弃阈值太低会让 DEADLINE_INFEASIBLE 永远触发不到。

    这条检查是实测出来的：早先 ``shed_critical_pressure=4.0`` 时，
    每条注定超时的请求都先在 CRITICAL 丢弃那里被拦下，
    于是调用方永远只能拿到笼统的「系统忙」。
    """
    # 阈值顺序本身合法（hard 2.5 < critical 3.5），3.5 × 1.5s = 5.25s 的排队
    # 仍然小于可行性边界 9s - 1.5s = 7.5s —— 于是 DEADLINE_INFEASIBLE 永远轮不到。
    report = check_config(governed(shed_critical_pressure=3.5))
    assert any("DEADLINE_INFEASIBLE" in p for p in report.problems)


def test_naive_retry_is_a_notice_not_a_problem():
    """对照组需要它，所以不能报成错误 —— 但必须提醒。"""
    report = check_config(governed(retry_budget_ratio=None))
    assert report.ok
    assert any("重试无预算" in n for n in report.notices)


def test_missing_ttl_jitter_is_a_notice():
    report = check_config(governed(cache_ttl_jitter=0.0))
    assert any("抖动" in n for n in report.notices)


def test_cpu_autoscale_is_a_notice():
    report = check_config(governed(autoscale_enabled=True, autoscale_signal="CPU"))
    assert any("CPU" in n for n in report.notices)


def test_priority_queue_without_aging_is_a_notice():
    report = check_config(governed(queue_policy=QueuePolicy.PRIORITY_AGING,
                                   aging_per_second=0.0))
    assert any("饿死" in n for n in report.notices)


# ======================================================================
# 构造期校验（比自检更强）
# ======================================================================
@pytest.mark.parametrize(
    "bad",
    [
        {"request_budget_ms": 1000},                       # 预算 ≤ 服务时间
        {"shed_soft_pressure": 5.0, "shed_hard_pressure": 1.0},   # 阈值乱序
    ],
)
def test_self_contradictory_config_cannot_even_be_constructed(bad):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GatewayConfig(**bad)


# ======================================================================
# 容量规划（§17）
# ======================================================================
def test_target_rps_uses_sixty_five_percent_waterline():
    """§2.3 结论：目标水位 65%，不是贴着容量跑。"""
    assert target_rps(0.65) == pytest.approx(DEFAULT_UPSTREAM.capacity_rps * 0.65)
    assert target_rps(0.65) < DEFAULT_UPSTREAM.capacity_rps * 0.7


def test_capacity_is_concurrency_over_service_time():
    config = GatewayConfig(upstream_concurrency=16, service_time_estimate_ms=2000)
    assert config.upstream_concurrency / (config.service_time_estimate_ms / 1000) == 8.0


# ======================================================================
# 热配置
# ======================================================================
def test_yaml_roundtrip_preserves_every_threshold(tmp_path):
    original = governed(queue_capacity=77, retry_budget_ratio=0.05)
    path = tmp_path / "gateway.yaml"
    path.write_text(config_to_yaml(original), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.queue_capacity == 77
    assert loaded.retry_budget_ratio == pytest.approx(0.05)


def test_unknown_key_in_yaml_raises_instead_of_being_ignored(tmp_path):
    """静默忽略一个阈值，等于让运维以为自己改了、但其实没改。"""
    path = tmp_path / "gateway.yaml"
    path.write_text("queue_capacity: 10\nedge_rpss: 100\n", encoding="utf-8")
    with pytest.raises(KeyError):
        load_config(path)


def test_missing_config_file_falls_back_to_defaults(tmp_path):
    loaded = load_config(tmp_path / "nope.yaml")
    assert loaded == GatewayConfig()


def test_all_upgrade_signals_are_covered():
    for signal in UpgradeSignal:
        config = governed(autoscale_enabled=True, autoscale_signal=signal.value)
        assert check_config(config).ok or signal is UpgradeSignal.CPU
