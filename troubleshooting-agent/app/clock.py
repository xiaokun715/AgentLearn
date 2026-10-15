"""可注入时钟 —— 让「超时」「预算耗尽」不用真的等。

排障系统里到处是时间：动作超时、总耗时预算、缓存结果的有效期。
如果这些都读 ``time.monotonic()``，测试就必须真等 —— 一个 5 秒超时的用例
会把整套测试拖到分钟级，于是没人愿意跑它，超时逻辑也就永远没被测过。

所以本 Demo 的所有时间读取都走 :class:`Clock` 接口，测试与演示可以换成
:class:`ManualClock`，把「过去 6 秒」变成一个函数调用。
"""
from __future__ import annotations

import time


class Clock:
    """默认实现：单调时钟。"""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:  # pragma: no cover - 演示不真睡
        time.sleep(seconds)


class ManualClock(Clock):
    """手动时钟：只有显式 :meth:`advance` 才会前进。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        self._now += seconds
        return self._now

    def sleep(self, seconds: float) -> None:
        # 手动时钟下「睡 N 秒」等价于「把表拨快 N 秒」，而且不消费真实时间
        self._now += seconds
