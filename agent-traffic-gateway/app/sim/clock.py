"""虚拟时钟 —— 整毫秒，单调前进。

为什么整毫秒而不是浮点秒：

仿真的核心循环要跑几十万次 tick，浮点累加会漂移（``0.1+0.2 != 0.3``），
而这种漂移会让「同样的输入跑两次得到不同结果」—— 场景无法回归，
README 里的数字也就不可信了。整毫秒是一个整数计数器，没有这个问题。
"""
from __future__ import annotations


class SimClock:
    """离散时间仿真时钟。

    时间只在 :meth:`advance` 里前进，而且步长固定。所有模块读到的
    ``now_ms`` 在同一个 tick 内完全相同 —— 这一点很重要：它让
    「同一时刻发生的多个事件」有一个确定的处理顺序（由代码顺序决定），
    而不是由调度器的随机性决定。
    """

    def __init__(self, tick_ms: int = 1) -> None:
        if tick_ms <= 0:
            raise ValueError("tick_ms 必须为正")
        self.tick_ms = tick_ms
        self.now_ms = 0

    def advance(self) -> int:
        self.now_ms += self.tick_ms
        return self.now_ms

    def seconds(self) -> float:
        return self.now_ms / 1000.0

    def reset(self) -> None:
        self.now_ms = 0
