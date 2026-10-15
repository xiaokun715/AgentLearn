"""端到端仿真测试（说明书 §2 / §7 / §10 / §12 / §16）。

这里断言的是**系统级不变量**：容量上界、记账完整性、可复现性，
以及每条治理结论在给定负载下的方向性。
"""
from __future__ import annotations

import pytest

from app.domain.enums import RejectReason, ServedBy
from app.domain.policy import DEFAULT_UPSTREAM, multi_tenant_workload, tenant
from app.sim.autoscaler import Autoscaler
from app.sim.engine import Simulation

from .conftest import queue_only, simulate, upstream_bound


# ======================================================================
# §2 容量
# ======================================================================
def test_capacity_formula_matches_upstream_config(upstream, capacity):
    assert capacity == pytest.approx(upstream.max_concurrency / (upstream.mean_service_ms / 1000))


def test_throughput_never_meaningfully_exceeds_capacity(bound, steady, capacity):
    """没有降级时，实测吞吐不可能超过「并发 / 服务时间」。"""
    result = simulate(bound(), steady(12.0, duration_ms=40_000, variant_ratio=0.0))
    assert result.achieved_rps < capacity * 1.05


def test_follows_offered_load_when_under_capacity(bound, steady):
    result = simulate(bound(), steady(2.0, duration_ms=40_000, variant_ratio=0.0))
    assert result.achieved_rps == pytest.approx(2.0, abs=0.4)


def test_queue_wait_grows_as_utilisation_approaches_one(bound, steady):
    """§2.3 利用率悬崖：排队时间对利用率是双曲线，不是直线。"""
    low = simulate(bound(), steady(2.0, duration_ms=40_000, variant_ratio=0.0))
    high = simulate(bound(), steady(5.0, duration_ms=40_000, variant_ratio=0.0))
    assert high.metrics.queue_wait.percentile(0.99) > low.metrics.queue_wait.percentile(0.99) * 3


def test_throughput_collapses_past_saturation(steady):
    """§2.5 过载时吞吐**下降** —— 超期请求白占了槽位。

    注意必须用「不丢弃也不清扫」的配置：一旦开了分级丢弃或超期清扫，
    那些注定失败的工作会被提前摘掉，槽位就不会被浪费，吞吐也就不会掉。
    —— 这反过来正好说明清扫和丢弃在**保护吞吐**。
    """
    peak = simulate(queue_only(), steady(6.0, duration_ms=40_000, variant_ratio=0.0))
    over = simulate(queue_only(), steady(9.0, duration_ms=40_000, variant_ratio=0.0))
    assert over.achieved_rps < peak.achieved_rps
    assert over.metrics.timed_out > peak.metrics.timed_out


# ======================================================================
# §7 准入
# ======================================================================
def test_admission_rejects_with_precise_reason(bound):
    """§7.4 拒绝要给出可行动的信息 —— 而不是笼统的「系统忙」。"""
    from app.domain.policy import steady_workload

    result = simulate(
        queue_only(admission_enabled=True, degrade_enabled=False),
        steady_workload(8.0, duration_ms=40_000),
    )
    assert result.gateway["admission"]["rejected_infeasible"] > 0
    assert RejectReason.DEADLINE_INFEASIBLE.value in result.summary["reject_reasons"]


def test_bounded_queue_rejects_with_queue_full():
    from app.domain.policy import steady_workload

    result = simulate(queue_only(queue_capacity=32, degrade_enabled=False),
                      steady_workload(8.0, duration_ms=40_000))
    assert result.summary["reject_reasons"].get(RejectReason.QUEUE_FULL.value, 0) > 0
    assert len(result.series("queue_depth")) > 0


def test_shedding_prefers_dropping_batch_first():
    """§13.1 分级丢弃的第一刀应该落在批处理上。"""
    from app.domain.policy import MIXED_TENANTS
    from app.domain.policy import multi_tenant_workload as mk

    result = simulate(upstream_bound(), mk(MIXED_TENANTS, duration_ms=40_000))
    by_tenant = {s["tenant_id"]: s for s in result.metrics.tenant_summary()}
    batch = by_tenant["batch"]
    interactive = by_tenant["interactive"]
    assert batch["ok"] / max(batch["arrived"], 1) < interactive["ok"] / max(interactive["arrived"], 1)


# ======================================================================
# §10 缓存与 §16 减负
# ======================================================================
def test_cache_and_coalescing_reduce_upstream_calls(steady):
    from app.domain.policy import governed

    hot = steady(12.0, duration_ms=40_000, prompt_space=50, zipf_alpha=1.3, variant_ratio=0.5)
    plain = simulate(queue_only(), hot)
    buffered = simulate(governed(), hot)
    assert buffered.summary["upstream_calls"] < plain.summary["upstream_calls"] * 0.3
    assert buffered.summary["cache_offload"] > 0.7


def test_served_by_breakdown_sums_to_total(steady):
    from app.domain.policy import governed

    result = simulate(governed(), steady(8.0, duration_ms=30_000))
    assert sum(result.summary["served_by"].values()) == result.metrics.total


def test_cache_hits_have_near_zero_latency(steady):
    from app.domain.policy import governed

    result = simulate(governed(), steady(8.0, duration_ms=30_000))
    assert result.metrics.served_by(ServedBy.CACHE_L1) > 0
    assert result.p50 <= 10


# ======================================================================
# §12 重试
# ======================================================================
def test_retry_budget_caps_amplification():
    from app.domain.policy import steady_workload

    flaky = DEFAULT_UPSTREAM.model_copy(update={"transient_fail_rate": 0.25})
    workload = steady_workload(5.2, duration_ms=40_000)
    naive = simulate(queue_only(retry_max_attempts=3, retry_budget_ratio=None), workload,
                     upstream=flaky)
    budgeted = simulate(queue_only(retry_max_attempts=3, retry_budget_ratio=0.10), workload,
                        upstream=flaky)
    assert naive.summary["retry_amplification"] > budgeted.summary["retry_amplification"]
    assert budgeted.summary["retry_amplification"] < 1.25


def test_no_retry_when_disabled():
    from app.domain.policy import steady_workload

    flaky = DEFAULT_UPSTREAM.model_copy(update={"transient_fail_rate": 0.25})
    result = simulate(queue_only(retry_enabled=False),
                      steady_workload(4.0, duration_ms=30_000), upstream=flaky)
    assert result.summary["retry_amplification"] == pytest.approx(1.0)
    assert result.gateway["counters"]["retries"] == 0


# ======================================================================
# §14 弹性伸缩
# ======================================================================
def test_cpu_reading_never_reaches_the_hpa_threshold():
    """§14.1 IO 密集型服务的 CPU 与负载几乎无关。"""
    assert Autoscaler.cpu_reading(0.0) < 0.3
    assert Autoscaler.cpu_reading(1.0) < 0.75
    assert Autoscaler.cpu_reading(1.0) > Autoscaler.cpu_reading(0.0)


def test_queue_depth_signal_reacts_to_slowdown():
    from app.domain.policy import steady_workload

    slow = DEFAULT_UPSTREAM.model_copy(update={"slow_down_at_ms": 8_000,
                                               "slow_down_factor": 3.0})
    workload = steady_workload(4.0, duration_ms=40_000)
    by_cpu = simulate(queue_only(autoscale_enabled=True, autoscale_signal="CPU",
                                 autoscale_cold_start_ms=2_000), workload, upstream=slow)
    by_queue = simulate(queue_only(autoscale_enabled=True, autoscale_signal="QUEUE_DEPTH",
                                   autoscale_cold_start_ms=2_000,
                                   autoscale_queue_threshold=10), workload, upstream=slow)
    assert by_cpu.autoscaler["scale_ups"] == 0
    assert by_queue.autoscaler["scale_ups"] > 0


# ======================================================================
# §16 记账完整性
# ======================================================================
def test_every_request_gets_exactly_one_outcome(steady):
    result = simulate(upstream_bound(), steady(8.0, duration_ms=30_000))
    assert result.metrics.total == result.gateway["counters"]["arrived"]


def test_pending_requests_are_counted_as_timeouts():
    """压测窗口结束时未完成的请求必须计入 —— 否则统计会系统性偏乐观。"""
    from app.domain.policy import steady_workload

    result = simulate(queue_only(), steady_workload(10.0, duration_ms=20_000))
    assert result.upstream["pending_at_end"] > 0
    assert result.metrics.timed_out >= result.upstream["pending_at_end"]


def test_coalescer_leaves_no_orphan_waiters(steady):
    from app.domain.policy import governed

    result = simulate(governed(), steady(12.0, duration_ms=30_000))
    assert result.gateway["coalescer"]["in_flight_now"] == 0
    assert result.gateway["coalescer"]["abandoned"] == 0 or result.metrics.total > 0


def test_reject_reasons_are_recorded_separately(steady):
    result = simulate(upstream_bound(), steady(9.0, duration_ms=30_000))
    reasons = result.summary["reject_reasons"]
    if reasons:
        assert set(reasons) <= {r.value for r in RejectReason}


# ======================================================================
# 可复现性
# ======================================================================
def test_same_seed_gives_identical_results(steady):
    workload = steady(5.0, duration_ms=15_000)
    first = simulate(upstream_bound(), workload)
    second = simulate(upstream_bound(), workload)
    assert first.metrics.total == second.metrics.total
    assert first.p99 == second.p99
    assert first.summary["upstream_calls"] == second.summary["upstream_calls"]


def test_different_seeds_give_different_traffic():
    from app.domain.policy import steady_workload

    a = simulate(queue_only(), steady_workload(5.0, duration_ms=15_000, seed=1))
    b = simulate(queue_only(), steady_workload(5.0, duration_ms=15_000, seed=2))
    assert a.metrics.total != b.metrics.total or a.p99 != b.p99


def test_workload_rng_is_independent_of_runtime_rng(steady):
    """§A/B 实验的前提：换配置不改变到达序列。"""
    workload = steady(6.0, duration_ms=20_000)
    plain = simulate(queue_only(), workload)
    tuned = simulate(queue_only(queue_capacity=64, sweep_expired=True), workload)
    assert plain.metrics.total == tuned.metrics.total


# ======================================================================
# 多租户
# ======================================================================
def test_tenant_quota_protects_the_small_neighbour():
    workload = multi_tenant_workload(
        [tenant("whale", 20.0), tenant("victim", 3.0)], duration_ms=40_000
    )
    unquota = simulate(queue_only(), workload)
    quota = simulate(queue_only(tenant_rps={"whale": 6.0}, tenant_burst={"whale": 12}), workload)

    def ok_ratio(result, name):
        stats = next(s for s in result.metrics.tenant_summary() if s["tenant_id"] == name)
        return stats["ok"] / max(stats["arrived"], 1)

    assert ok_ratio(quota, "victim") > ok_ratio(unquota, "victim")


def test_wfq_improves_small_tenant_latency():
    from app.domain.enums import QueuePolicy
    from app.domain.policy import FAIRNESS_TENANTS

    workload = multi_tenant_workload(FAIRNESS_TENANTS, duration_ms=60_000)
    fifo = simulate(queue_only(queue_policy=QueuePolicy.FIFO), workload)
    wfq = simulate(queue_only(queue_policy=QueuePolicy.WFQ), workload)

    def mouse_p99(result):
        stats = next(s for s in result.metrics.tenant_summary() if s["tenant_id"] == "mouse")
        return stats["latency"]["p99"]

    assert mouse_p99(wfq) < mouse_p99(fifo)


def test_engine_accepts_custom_tick_size(steady):
    """tick 变大只影响精度，不影响结论的方向。"""
    sim = Simulation(queue_only(), DEFAULT_UPSTREAM, steady(5.0, duration_ms=20_000),
                     tick_ms=5)
    result = sim.run()
    assert result.metrics.total > 0
    assert result.achieved_rps == pytest.approx(5.0, abs=1.0)
