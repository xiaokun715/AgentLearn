"""治理部件的单元测试（说明书 §6 / §8 / §9 / §10 / §11 / §12）。

这一层测的是**每个机制自己的不变量**，不涉及端到端行为：

- 令牌桶的补桶与突发；
- 有界队列的快速失败、老化、WFQ 的虚拟时间；
- 合并的等待者绝不泄漏；
- 熔断状态机；
- 重试预算的「永不自我放大」。
"""
from __future__ import annotations

import pytest

from app.domain.enums import CircuitState, Priority, QueuePolicy, ServedBy
from app.domain.models import GatewayConfig, Request
from app.gateway.cache import TieredCache
from app.gateway.coalescer import Coalescer
from app.gateway.limiter import RateLimiterSet, SlidingWindowCounter, TokenBucket
from app.gateway.protect import CircuitBreaker, RetryBudget, UpstreamSemaphore
from app.gateway.scheduler import BoundedScheduler
from app.sim.rng import Rng


def make_request(rid: str, *, tenant: str = "t1", priority: Priority = Priority.STANDARD,
                 arrived: int = 0, deadline: int = 60_000, weight: float = 1.0,
                 key: str = "k", semantic: str = "s") -> Request:
    return Request(
        request_id=rid, tenant_id=tenant, priority=priority,
        cache_key=key, semantic_key=semantic,
        arrived_ms=arrived, deadline_ms=deadline, tenant_weight=weight,
    )


# ======================================================================
# §6 限流
# ======================================================================
def test_token_bucket_allows_burst_then_throttles():
    """突发额度之内全放行，之后被速率限制。"""
    bucket = TokenBucket(rate_per_sec=10, burst=20)
    allowed = sum(1 for _ in range(20) if bucket.try_acquire(0))
    assert allowed == 20
    assert not bucket.try_acquire(0)


def test_token_bucket_refills_over_time():
    bucket = TokenBucket(rate_per_sec=100, burst=10)
    for _ in range(10):
        bucket.try_acquire(0)
    assert not bucket.try_acquire(0)
    assert bucket.try_acquire(50)      # 50ms × 0.1 token/ms = 5 个令牌


def test_sliding_window_is_not_fooled_by_boundaries():
    """固定窗口在边界处可以放行两倍配额；滑动窗口不会被骗。"""
    window = SlidingWindowCounter(limit=10, window_ms=1000)
    for _ in range(10):
        assert window.allow(999)       # 窗口末尾打满
    assert not window.allow(1000)      # 固定窗口会在这一刻重置 —— 滑动窗口不会
    assert window.allow(1001 + 1000)   # 但过了窗口就能再放


def test_rate_limiter_set_rejects_hitting_tenant_quota():
    config = GatewayConfig(edge_rps=10_000, edge_burst=10_000,
                           tenant_rps={"t1": 1.0}, tenant_burst={"t1": 1})
    limiters = RateLimiterSet(config)
    assert limiters.check(make_request("a"), 0) is None
    from app.domain.enums import RejectReason

    assert limiters.check(make_request("b"), 0) is RejectReason.TENANT_QUOTA_EXCEEDED


# ======================================================================
# §8 队列与调度
# ======================================================================
def test_bounded_scheduler_rejects_when_full():
    scheduler = BoundedScheduler(GatewayConfig(queue_capacity=2))
    assert scheduler.enqueue(make_request("a"), 0)
    assert scheduler.enqueue(make_request("b"), 0)
    assert not scheduler.enqueue(make_request("c"), 0)
    assert scheduler.rejected_full == 1


def test_fifo_returns_earliest_arrival():
    """FIFO 的前提是「按到达顺序入队」—— 引擎保证了这一点。"""
    scheduler = BoundedScheduler(GatewayConfig(queue_policy=QueuePolicy.FIFO))
    scheduler.enqueue(make_request("first", arrived=10), 10)
    scheduler.enqueue(make_request("second", arrived=100), 100)
    assert scheduler.pop(200).request_id == "first"
    assert scheduler.pop(200).request_id == "second"


def test_priority_queue_serves_high_priority_first():
    scheduler = BoundedScheduler(GatewayConfig(queue_policy=QueuePolicy.PRIORITY_AGING,
                                               aging_per_second=0.0))
    scheduler.enqueue(make_request("batch", priority=Priority.BATCH), 0)
    scheduler.enqueue(make_request("interactive", priority=Priority.INTERACTIVE), 0)
    assert scheduler.pop(0).request_id == "interactive"


def test_aging_prevents_low_priority_starvation():
    """§8.2 低优先级等得够久之后应该被提升 —— 否则会被永久饿死。"""
    scheduler = BoundedScheduler(GatewayConfig(queue_policy=QueuePolicy.PRIORITY_AGING,
                                               aging_per_second=1.0))
    scheduler.enqueue(make_request("old_batch", priority=Priority.BATCH, arrived=0), 0)
    scheduler.enqueue(make_request("new_critical", priority=Priority.CRITICAL, arrived=5_000), 0)
    # 老了 5 秒的批处理（0 + 1.0×5 = 5）压过了刚到的关键请求（4 + 0）
    assert scheduler.pop(5_000).request_id == "old_batch"


def test_wfq_serves_small_flow_promptly():
    """§8.4 WFQ 的核心性质：低速率流的请求不应该排在高速率流的一整队后面。"""
    scheduler = BoundedScheduler(GatewayConfig(queue_policy=QueuePolicy.WFQ))
    for index in range(50):
        scheduler.enqueue(make_request(f"w{index:02d}", tenant="whale", arrived=index), 0)
    scheduler.enqueue(make_request("m0", tenant="mouse", arrived=10), 0)
    # mouse 只发了 1 个请求，它不应该排到 whale 的 50 个后面
    popped = [scheduler.pop(100).request_id for _ in range(5)]
    assert "m0" in popped
    assert popped.index("m0") <= 1


def test_sweep_expired_removes_only_overdue():
    scheduler = BoundedScheduler(GatewayConfig())
    scheduler.enqueue(make_request("fresh", deadline=10_000), 0)
    scheduler.enqueue(make_request("stale", deadline=100), 0)
    expired = scheduler.sweep_expired(500)
    assert [r.request_id for r in expired] == ["stale"]
    assert scheduler.depth == 1


# ======================================================================
# §9 请求合并
# ======================================================================
def test_coalescer_merges_same_key():
    coalescer = Coalescer()
    leader = make_request("leader", key="k1")
    coalescer.start(leader, 0)
    assert coalescer.join(make_request("f1", key="k1"), 1)
    assert coalescer.join(make_request("f2", key="k1"), 2)
    assert not coalescer.join(make_request("other", key="k2"), 3)
    waiters = coalescer.complete("k1")
    assert {w.request_id for w in waiters} == {"f1", "f2"}


def test_coalescer_refuses_non_coalescable():
    """§9.2 带副作用的请求不能被合并。"""
    coalescer = Coalescer()
    coalescer.start(make_request("leader", key="k1"), 0)
    side_effect = make_request("writer", key="k1").model_copy(update={"coalescable": False})
    assert not coalescer.join(side_effect, 1)


def test_abandon_returns_every_waiter():
    """leader 失败时，等待者必须被交出来 —— 否则它们永远挂起。"""
    coalescer = Coalescer()
    coalescer.start(make_request("leader", key="k1"), 0)
    coalescer.join(make_request("f1", key="k1"), 1)
    assert len(coalescer.abandon("k1")) == 1
    assert coalescer.inflight_keys == 0


def test_drain_all_is_a_safety_net():
    coalescer = Coalescer()
    coalescer.start(make_request("leader", key="k1"), 0)
    coalescer.join(make_request("f1", key="k1"), 1)
    assert len(coalescer.drain_all()) == 1
    assert coalescer.inflight_keys == 0


# ======================================================================
# §10 缓存
# ======================================================================
def test_cache_hits_exact_key_at_l1():
    config = GatewayConfig(l1_capacity=8, l2_capacity=8, semantic_cache_enabled=False)
    cache = TieredCache(config, Rng(1))
    req = make_request("a", key="kx", semantic="sx")
    cache.store(req, 0)
    assert cache.lookup(make_request("b", key="kx", semantic="sy"), 1) is ServedBy.CACHE_L1


def test_semantic_cache_serves_a_different_surface_form():
    """§10.1 语义缓存能命中**另一个表面形式** —— 这是精确缓存做不到的。"""
    config = GatewayConfig(l1_capacity=8, l2_capacity=8, semantic_cache_enabled=True)
    cache = TieredCache(config, Rng(1))
    cache.store(make_request("a", key="k_v0", semantic="intent"), 0)
    hit = cache.lookup(make_request("b", key="k_v1", semantic="intent"), 1)
    assert hit is ServedBy.CACHE_SEMANTIC


def test_disabling_semantic_cache_loses_that_hit():
    config = GatewayConfig(l1_capacity=8, l2_capacity=8, semantic_cache_enabled=False)
    cache = TieredCache(config, Rng(1))
    cache.store(make_request("a", key="k_v0", semantic="intent"), 0)
    assert cache.lookup(make_request("b", key="k_v1", semantic="intent"), 1) is None


def test_ttl_jitter_spreads_expiry():
    """§10.2 固定 TTL 会让缓存雪崩变成周期性事件 —— 抖动必须真的存在。"""
    config = GatewayConfig(l1_capacity=64, l2_capacity=64, cache_ttl_ms=10_000,
                           cache_ttl_jitter=0.25, semantic_cache_enabled=False)
    cache = TieredCache(config, Rng(7))
    expiries = set()
    for index in range(20):
        req = make_request(f"r{index}", key=f"k{index}")
        cache.store(req, 0)
        expiries.add(cache.l2._items[f"k{index}"].expires_ms)
    assert len(expiries) > 1
    assert all(7_500 <= value <= 12_500 for value in expiries)


def test_no_jitter_means_one_expiry_moment():
    config = GatewayConfig(l1_capacity=64, l2_capacity=64, cache_ttl_ms=10_000,
                           cache_ttl_jitter=0.0, semantic_cache_enabled=False)
    cache = TieredCache(config, Rng(7))
    for index in range(5):
        cache.store(make_request(f"r{index}", key=f"k{index}"), 0)
    assert {entry.expires_ms for entry in cache.l2._items.values()} == {10_000}


def test_negative_cache_blocks_repeat_failures():
    config = GatewayConfig(l1_capacity=8, l2_capacity=8, cache_ttl_ms=10_000)
    cache = TieredCache(config, Rng(1))
    req = make_request("a", key="bad")
    cache.store_negative(req, 0)
    assert cache.negative_hit(make_request("b", key="bad"), 1)
    assert not cache.negative_hit(make_request("c", key="bad"), 10_000)


# ======================================================================
# §11 并发信号量
# ======================================================================
def test_semaphore_is_a_hard_ceiling():
    semaphore = UpstreamSemaphore(limit=2)
    assert semaphore.try_acquire() and semaphore.try_acquire()
    assert not semaphore.try_acquire()
    assert semaphore.denied_total == 1
    semaphore.release()
    assert semaphore.try_acquire()


def test_semaphore_limit_can_be_raised_at_runtime():
    """§14 弹性伸缩调的就是这个数。"""
    semaphore = UpstreamSemaphore(limit=1)
    semaphore.try_acquire()
    assert not semaphore.try_acquire()
    semaphore.set_limit(4)
    assert semaphore.try_acquire()


# ======================================================================
# §12 重试预算与熔断
# ======================================================================
def test_retry_budget_is_proportional_to_successes():
    """§12.2 预算的本质：重试数 ≤ ratio × 成功数，所以永不自我放大。"""
    budget = RetryBudget(ratio=0.1, burst=1000, initial=0.0)
    for _ in range(1000):
        budget.on_success()
    granted = sum(1 for _ in range(200) if budget.allow())
    assert granted == pytest.approx(100, abs=2)


def test_retry_budget_caps_at_burst():
    budget = RetryBudget(ratio=0.1, burst=5, initial=0.0)
    for _ in range(1000):
        budget.on_success()
    assert sum(1 for _ in range(100) if budget.allow()) == 5


def test_no_budget_means_naive_retry():
    budget = RetryBudget(ratio=None)
    assert all(budget.allow() for _ in range(50))


def test_breaker_opens_after_enough_failures():
    config = GatewayConfig(breaker_enabled=True, breaker_window_ms=1000,
                           breaker_min_samples=10, breaker_failure_ratio=0.5,
                           breaker_cooldown_ms=500)
    breaker = CircuitBreaker(config)
    for index in range(10):
        breaker.record(index >= 6, index)          # 10 次里 6 次失败
    assert breaker.state is CircuitState.OPEN
    assert not breaker.allow(11)
    assert breaker.short_circuited == 1


def test_breaker_needs_min_samples():
    """§12.4 低流量时不能用比例判断 —— 1 次失败 ≠ 100% 失败率。"""
    config = GatewayConfig(breaker_enabled=True, breaker_window_ms=10_000,
                           breaker_min_samples=20, breaker_failure_ratio=0.5)
    breaker = CircuitBreaker(config)
    for index in range(5):
        breaker.record(False, index)
    assert breaker.state is CircuitState.CLOSED


def test_breaker_half_open_then_closes():
    config = GatewayConfig(breaker_enabled=True, breaker_window_ms=1000,
                           breaker_min_samples=4, breaker_failure_ratio=0.5,
                           breaker_cooldown_ms=100, breaker_half_open_probes=2)
    breaker = CircuitBreaker(config)
    for index in range(4):
        breaker.record(False, index)
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow(200)                       # 冷却结束 → HALF_OPEN
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.record(True, 201)
    breaker.record(True, 202)
    assert breaker.state is CircuitState.CLOSED
