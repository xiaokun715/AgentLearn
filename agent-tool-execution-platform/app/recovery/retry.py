"""重试控制器 —— 说明书 §36 Retry Policy（Exponential Backoff + Jitter）。

::

    attempt:   0      1      2      3      ...
    backoff:  1s  -> 2s  -> 4s  -> 8s   ... 封顶 max_backoff_ms
              ↑ 每一档再乘 (1 ± jitter_ratio)

为什么必须加抖动（§36 原文的「服务雪崩」）
------------------------------------------
没有抖动的退避是**确定性的**：``1s / 2s / 4s`` 对每一个失败调用都一样。
于是当一次下游抖动（比如 503 风暴）让 1000 个 Agent 同时失败时，
它们会在**同一毫秒**一起重试 —— 下游刚恢复就被第二波打垮，再失败，再同步重试，
形成周期性的脉冲式雪崩。加了抖动之后，重试时刻被摊平到
``[base*(1-r), base*(1+r)]`` 区间内，脉冲被打散成平缓的曲线。

``random_source`` 为什么必须可注入
---------------------------------
抖动一旦引入随机数，「退避 1000ms 后重试」这句话在推演里就既不可复现也不可断言。
把随机源抽成 :class:`~typing.Callable` 之后，演示/推演可以注入
``lambda: 0.5``（抖动系数恰好 1.0，退避值就是干净的 1000/2000/4000/8000），
生产环境仍用 ``random.random``。**可观测的前提是可复现。**
"""
from __future__ import annotations

import random
from typing import Callable, Optional

from ..domain.enums import ErrorType
from ..domain.policy import RetryPolicy


class RetryController:
    """退避计算 + 「还该不该再试一次」的裁决（§36）。

    :param random_source: 返回 ``[0, 1)`` 的随机源；缺省 ``random.random``。
        注入固定值可以让退避序列完全确定（见模块 docstring）。
    """

    def __init__(self, *, random_source: Optional[Callable[[], float]] = None) -> None:
        self._random: Callable[[], float] = random_source or random.random

    # ==================================================================
    # 退避
    # ==================================================================
    def backoff_ms(self, attempt: int, policy: RetryPolicy) -> int:
        """算出第 ``attempt`` 次失败之后应该等多久（毫秒）—— §36 的公式。

        ::

            base   = min(initial_backoff_ms * multiplier ** attempt, max_backoff_ms)
            jitter = 1 + jitter_ratio * (2 * rand - 1)      # rand ∈ [0, 1) -> [-1, 1]
            result = clamp(base * jitter, 0, max_backoff_ms)

        ``attempt`` 从 0 开始：第 0 次失败按 ``initial_backoff_ms`` 退避，
        这样「第一次重试等 1s」和配置里的字面值对得上，不必在脑子里做减一。

        最后那次 ``clamp`` 是刻意的：如果只在乘抖动**之前**封顶，
        抖动会把结果顶到 ``max_backoff_ms`` 之上（例如 30s * 1.3 = 39s），
        上限就名存实亡了 —— 上限的语义是「无论怎么抖都不会超过它」。
        """
        if attempt < 0:
            raise ValueError(f"attempt 不能为负: {attempt}")

        base = float(policy.initial_backoff_ms) * (float(policy.multiplier) ** attempt)
        base = min(base, float(policy.max_backoff_ms))

        # 抖动区间 [1 - jitter_ratio, 1 + jitter_ratio]；jitter_ratio=0 时退化为纯指数退避
        rand = float(self._random())
        jitter = 1.0 + float(policy.jitter_ratio) * (2.0 * rand - 1.0)

        return int(max(0.0, min(base * jitter, float(policy.max_backoff_ms))))

    # ==================================================================
    # 裁决
    # ==================================================================
    def should_retry(
        self,
        *,
        attempt: int,
        policy: RetryPolicy,
        error_type: ErrorType,
    ) -> bool:
        """还能不能再试一次 —— **两个条件必须同时满足**。

        1. ``attempt + 1 < policy.max_attempts``：次数没用完。
           ``attempt`` 是「已经失败了几次」，所以第 0 次失败时还剩 ``max_attempts`` 次机会。
        2. ``policy.allows(error_type)``：这类错误**本身**值得重试（§36 的 ``retry_on``）。

        第 2 条是防「无效重放」的闸门：``permission_error`` / ``validation_error``
        重试多少次结果都一样，只会把失败延迟暴露并浪费资源。
        两条都过不了时，:class:`~app.recovery.policy.RecoveryPolicyEngine`
        会把动作降级成 ``ABORT``（§75 的「不会简单地无限 Retry」）。
        """
        if attempt + 1 >= policy.max_attempts:
            return False
        return policy.allows(error_type)

    def describe(self, attempt: int, policy: RetryPolicy) -> dict:
        """给日志/审计用的一行摘要 —— 让「为什么退避这么久」可以直接读出来。

        ``allowed`` 只表达**次数维度**是否还有余量；是否真重试还要看错误类型，
        由 :meth:`should_retry` 决定（这里不传 ``error_type``，故不混为一谈）。
        """
        return {
            "attempt": attempt,
            "next_backoff_ms": self.backoff_ms(attempt, policy),
            "max_attempts": policy.max_attempts,
            "allowed": attempt + 1 < policy.max_attempts,
        }
