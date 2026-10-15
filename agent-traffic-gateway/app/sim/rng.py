"""可复现的随机数源 —— 确定性是这份 Demo 能被断言的前提。

说明书里的每一条结论（合并放大比、重试放大倍数、公平性对比）
都必须是**可复现**的，否则它们就只是「我跑过一次看到大概是这个数」。

所以：所有随机性都走这一个入口，种子固定；需要对照实验时
（例如「同一批流量，只换调度策略」），两个实验**共用同一个种子** ——
这样比较的就是策略的差异，而不是流量的差异。
"""
from __future__ import annotations

import math
import random


class Rng:
    """对 ``random.Random`` 的薄封装：让随机性的来源可以被审计与替换。"""

    def __init__(self, seed: int = 20261016) -> None:
        self.seed = seed
        self._r = random.Random(seed)

    # ------------------------------------------------------------------
    def uniform(self, low: float, high: float) -> float:
        return self._r.uniform(low, high)

    def exponential(self, mean: float) -> float:
        """指数分布 —— 泊松到达的间隔时间分布（§2.3 建模的前提）。"""
        if mean <= 0:
            return 0.0
        return self._r.expovariate(1.0 / mean)

    def lognormal(self, mean: float, sigma: float) -> float:
        """对数正态 —— §3.1「服务时间方差极大」的量化方式。

        取对数正态而不是正态，是因为服务时间**不能为负**，
        而且真实分布本来就是右偏的：大多数请求很快，少数请求非常慢。
        """
        if sigma <= 0:
            return mean
        mu = math.log(max(mean, 1e-9)) - (sigma ** 2) / 2.0
        return self._r.lognormvariate(mu, sigma)

    def bernoulli(self, p: float) -> bool:
        return self._r.random() < p

    def weighted_index(self, weights: list[float]) -> int:
        """按权重抽一个下标（Zipf 热点流量用它）。"""
        return self._r.choices(range(len(weights)), weights=weights, k=1)[0]

    def choice(self, items: list) -> object:
        return self._r.choice(items)
