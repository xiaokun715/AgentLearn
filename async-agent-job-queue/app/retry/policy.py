"""Retry Policy（设计说明书 §24-25）。

核心结论：不是所有异常都应该 Retry。
- LLM timeout / HTTP 500 / Rate Limit  -> 可重试（瞬时错误）
- Invalid Prompt / Permission Denied   -> 不可重试（改了也没用）

``RetryableError`` 是项目内「可重试」的显式信号；默认策略：
- 抛出 RetryableError -> 重试
- 其余 Exception      -> 不重试（直接 FAILED / DLQ）
"""
from __future__ import annotations

import random

from ..domain.exceptions import NonRetryableError, RetryableError  # re-export

__all__ = ["RetryableError", "NonRetryableError"]


class RetryPolicy:
    """判断某个异常是否应该重试。可继承扩展（例如按异常类型/错误码分类）。"""

    def should_retry(self, error: BaseException) -> bool:
        if isinstance(error, NonRetryableError):
            return False
        if isinstance(error, RetryableError):
            return True
        # 默认策略：只有显式 RetryableError 才重试。
        # 真实的系统里应再细分：HTTP 429/5xx 可重试，4xx 不可重试。
        return False


def compute_backoff(
    retry_count: int,
    *,
    base: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.5,
) -> float:
    """Exponential Backoff + Jitter（§25）。

    delay = min(max_delay, base * 2 ** retry_count)
    delay += random.uniform(0, jitter)

    Jitter 的作用：避免 1000 个 Worker 同时重试造成 Retry Storm。
    """
    delay = min(max_delay, base * (2 ** retry_count))
    delay += random.uniform(0, jitter)
    return delay
