"""实验 2 / 3：Exponential Backoff 与 Retry-After（§16~§19, §38）。"""
from __future__ import annotations

import random

import pytest

from app.webhook.retry import DEFAULT_RETRY_STATUS_CODES, RetryPolicy


def test_retry_status_codes_table():
    """§18：只有特定状态码才重试。"""
    policy = RetryPolicy()
    # 不应重试
    assert not policy.should_retry(status_code=200)
    assert not policy.should_retry(status_code=400)
    assert not policy.should_retry(status_code=401)
    assert not policy.should_retry(status_code=403)
    assert not policy.should_retry(status_code=404)
    # 应该重试
    for code in (408, 429, 500, 502, 503, 504):
        assert policy.should_retry(status_code=code), f"{code} 应重试"
    # 网络/超时异常
    assert policy.should_retry(error=True)


def test_retry_status_codes_default_set():
    assert DEFAULT_RETRY_STATUS_CODES == frozenset({408, 429, 500, 502, 503, 504})


def test_exponential_backoff_sequence():
    """§17：delay = min(max_delay, base_delay * 2^(attempt-1))。"""
    policy = RetryPolicy(base_delay=1.0, max_delay=300.0, jitter=0.0)
    assert policy.backoff(1) == 1
    assert policy.backoff(2) == 2
    assert policy.backoff(3) == 4
    assert policy.backoff(4) == 8
    assert policy.backoff(5) == 16


def test_backoff_capped_by_max_delay():
    policy = RetryPolicy(base_delay=1.0, max_delay=10.0, jitter=0.0)
    assert policy.backoff(6) == 10.0  # 2^5=32 被 cap 到 10
    assert policy.backoff(10) == 10.0


def test_jitter_spreads_retries():
    """§17：抖动避免 Thundering Herd。"""
    policy = RetryPolicy(base_delay=1.0, jitter=0.25)
    random.seed(42)
    delays = [policy.delay_with_jitter(attempt=1) for _ in range(100)]
    assert min(delays) >= 1.0
    assert max(delays) <= 1.25
    assert len(set(delays)) > 1  # 有随机性


def test_retry_after_wins_over_backoff():
    """§38：429 + Retry-After 时尊重服务端 backpressure。"""
    policy = RetryPolicy(base_delay=1.0, jitter=0.0)
    delay = policy.next_retry_delay(attempt=3, retry_after=30)
    assert delay == 30.0  # 而不是退避 4s


def test_compute_next_retry_at_is_future():
    policy = RetryPolicy(base_delay=1.0, jitter=0.0)
    from app.domain.event import utcnow

    now = utcnow()
    next_at = policy.compute_next_retry_at(attempt=2, now=now)
    assert (next_at - now).total_seconds() == 2.0


def test_max_attempts_exhaustion():
    """§20：达到 max_attempts 即判定用尽，进 DLQ。"""
    policy = RetryPolicy(max_attempts=5)
    assert not policy.is_exhausted(1)
    assert not policy.is_exhausted(4)
    assert policy.is_exhausted(5)


def test_429_is_retryable_400_is_not():
    policy = RetryPolicy()
    assert policy.should_retry(status_code=429)
    assert not policy.should_retry(status_code=400)
