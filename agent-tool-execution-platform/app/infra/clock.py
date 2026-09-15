"""可注入时钟 —— 让「租约过期」「心跳超时」这类时间行为可以被确定性地演示和验证。

这是刻意抽出来的一层：整个平台的可靠性语义几乎都建立在「时间过去了多久」之上
（Lease TTL §14、Heartbeat 间隔 §15、Retry Backoff §36、Agent Heartbeat §16）。
如果代码直接调 ``time.time()``，那么「Worker 崩溃、租约过期、Reaper 接管」这一整条
链路就只能靠 ``sleep`` 去验证，既慢又飘。

于是引入 :class:`Clock`：生产用 :class:`SystemClock`，演示/推演用 :class:`ManualClock`
把时间「拨快」，一秒钟就能把 30s 的租约过期走完。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone


class Clock:
    """时钟抽象。接口保持极窄：只要 ``time()``（epoch 秒）与 ``now()``。"""

    def time(self) -> float:
        raise NotImplementedError

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.time(), tz=timezone.utc)

    def sleep(self, seconds: float) -> None:  # pragma: no cover - 默认实现
        time.sleep(seconds)


class SystemClock(Clock):
    """真实挂钟。"""

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ManualClock(Clock):
    """手动时钟：只有显式 :meth:`advance` 才会前进。

    ``sleep`` 被实现成「直接拨快」而不是真睡 —— 于是基于时钟的等待逻辑
    （轮询等待异步 Tool、等待租约过期）在测试/演示里瞬时完成。
    """

    def __init__(self, start: float | None = None) -> None:
        self._now = start if start is not None else time.time()

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        """把时间向前拨 ``seconds`` 秒，返回新的时间戳。"""
        self._now += seconds
        return self._now

    def sleep(self, seconds: float) -> None:
        # 手动时钟下 sleep 不消耗真实时间，只推进逻辑时间
        self.advance(seconds)
