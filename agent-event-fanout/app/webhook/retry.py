"""重试策略（设计说明书 §16~§19, §38）。

- **Exponential Backoff**（§17）：``delay = min(max_delay, base_delay * 2^(attempt-1))``
- **Jitter**（§17）：加随机抖动，避免 10000 个 Webhook 同时失败同时重试
  （Thundering Herd）。
- **Retry-After**（§38）：客户返回 429 且带 ``Retry-After`` 时，
  尊重服务端显式 backpressure，而不是无脑指数退避。
- 不是所有错误都重试（§18）：2xx/400/401/403/404 不重试；
  408/429/5xx/超时/连接错误重试。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..domain.event import utcnow

# 设计 §18：默认「应该 Retry」的 HTTP 状态码
DEFAULT_RETRY_STATUS_CODES: set[int] = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 300.0
    # 抖动比例：delay = backoff * (1 + random.uniform(0, jitter))
    jitter: float = 0.25
    retry_status_codes: set[int] = field(default_factory=lambda: set(DEFAULT_RETRY_STATUS_CODES))

    def should_retry(self, *, status_code: int | None = None, error: bool = False) -> bool:
        """§18：只有可恢复错误才重试。``error=True`` 表示网络/超时类异常。"""
        if error:
            return True
        if status_code is None:
            return False
        return status_code in self.retry_status_codes

    def backoff(self, attempt: int) -> float:
        """§17：``min(max_delay, base_delay * 2^(attempt-1))``，attempt 从 1 开始。"""
        return min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))

    def delay_with_jitter(self, attempt: int) -> float:
        """退避 + 随机抖动。"""
        backoff = self.backoff(attempt)
        return backoff * (1 + random.uniform(0, self.jitter))

    def next_retry_delay(
        self,
        *,
        attempt: int,
        retry_after: int | None = None,
        now: datetime | None = None,
    ) -> float:
        """下一次重试的延迟秒数。

        优先尊重 ``Retry-After``（§38 服务端显式 backpressure），
        否则用指数退避 + 抖动（§17）。
        """
        if retry_after is not None:
            return float(retry_after)
        return self.delay_with_jitter(attempt)

    def compute_next_retry_at(
        self,
        *,
        attempt: int,
        retry_after: int | None = None,
        now: datetime | None = None,
    ) -> datetime:
        now = now or utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        delay = self.next_retry_delay(attempt=attempt, retry_after=retry_after, now=now)
        return now + timedelta(seconds=delay)

    def is_exhausted(self, attempt: int) -> bool:
        """§20：超过 max_attempts 即进 DLQ，不能无限重试。"""
        return attempt >= self.max_attempts
