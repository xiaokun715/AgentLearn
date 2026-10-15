"""多级限流（说明书 §6）。

三种基础算法里，本模块实现两种：

- :class:`TokenBucket` —— 同时表达「速率」和「突发」两个概念（§6.1），
  用于全局边缘限流与租户配额；
- :class:`SlidingWindowCounter` —— 用于需要按时间窗口解释的配额，
  而且它顺带说明了**为什么不能用固定窗口**（§6.1 的边界问题）。

限流键的分层（§6.2）在 :class:`RateLimiterSet` 里：

    L1 全局边缘  → 保护网关自身
    L2 租户       → 邻居隔离
    L3/L4（上游）→ 由并发信号量承担，不在这里（见 protect.py）
"""
from __future__ import annotations

from collections import deque

from ..domain.enums import RejectReason
from ..domain.models import GatewayConfig, Request

#: 令牌桶的补桶精度：内部按毫秒补，不需要更高的精度
_MS_PER_SECOND = 1000.0


class TokenBucket:
    """令牌桶。

    ``rate_per_sec`` 是稳态速率，``burst`` 是允许的突发量。
    两个参数缺一不可：

    - 只有速率没有突发 → 秒级抖动都会被拒（对正常流量不友好）；
    - 只有突发没有速率 → 退化成计数器，洪峰照样打过去。

    **补桶是惰性的**（在 ``try_acquire`` 里按时间差补），
    所以它不需要定时器 —— 这一点在高并发下很重要：
    如果每个桶都要一个定时器，几百万个键就是几百万个定时器。
    """

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate_per_ms = rate_per_sec / _MS_PER_SECOND
        self.burst = float(burst)
        self.tokens = float(burst)
        self.last_ms: int | None = None
        self.acquired = 0
        self.rejected = 0

    def _refill(self, now_ms: int) -> None:
        if self.last_ms is None:
            self.last_ms = now_ms
            return
        elapsed = now_ms - self.last_ms
        if elapsed <= 0:
            return
        self.last_ms = now_ms
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate_per_ms)

    def try_acquire(self, now_ms: int, tokens: float = 1.0) -> bool:
        self._refill(now_ms)
        if self.tokens >= tokens:
            self.tokens -= tokens
            self.acquired += 1
            return True
        self.rejected += 1
        return False

    @property
    def saturation(self) -> float:
        """桶的空虚程度：0 = 满，1 = 空。用作限流层的饱和度指标。"""
        return 1.0 - (self.tokens / self.burst if self.burst else 0.0)


class SlidingWindowCounter:
    """滑动窗口计数器。

    实现方式是「窗口内的时间戳队列」而不是「分片计数」，因为前者
    更容易讲清楚它与固定窗口的区别：**它看的是真正的过去 N 毫秒**，
    而固定窗口只看「当前这一格」，于是在格子边界上可以放行两倍配额
    （00:59 打满 + 01:00 打满 = 两秒内 2× 配额，§6.1）。
    """

    def __init__(self, limit: int, window_ms: int) -> None:
        self.limit = limit
        self.window_ms = window_ms
        self._marks: deque[int] = deque()
        self.rejected = 0

    def allow(self, now_ms: int) -> bool:
        cutoff = now_ms - self.window_ms
        while self._marks and self._marks[0] <= cutoff:
            self._marks.popleft()
        if len(self._marks) < self.limit:
            self._marks.append(now_ms)
            return True
        self.rejected += 1
        return False

    @property
    def used(self) -> int:
        return len(self._marks)


class RateLimiterSet:
    """§6.2 的多层限流。

    一个刻意的设计取舍：**L1 的阈值要「略高于」集群总容量**。

    如果 L1 就等于容量，它会在准入控制之前先把正常流量拒掉 ——
    而准入控制比它精确得多（它知道 deadline，L1 不知道）。
    所以 L1 的职责是挡住**明显的**洪峰（比如 10 倍突发），
    精细的取舍交给 §7 的准入控制。
    """

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.edge = TokenBucket(config.edge_rps, config.edge_burst)
        self._tenant_buckets: dict[str, TokenBucket] = {}
        #: L3：按租户 + API key 维度，防止单租户用大量 key 绕过（§6.4）
        self._key_buckets: dict[tuple[str, str], TokenBucket] = {}

    # ------------------------------------------------------------------
    def _tenant_bucket(self, tenant_id: str) -> TokenBucket | None:
        rate = self.config.tenant_rps.get(tenant_id)
        if rate is None:
            return None
        bucket = self._tenant_buckets.get(tenant_id)
        if bucket is None:
            burst = self.config.tenant_burst.get(tenant_id, max(1, int(rate)))
            bucket = TokenBucket(rate, burst)
            self._tenant_buckets[tenant_id] = bucket
        return bucket

    # ------------------------------------------------------------------
    def check(self, req: Request, now_ms: int) -> RejectReason | None:
        """依次过 L1 → L2。返回 ``None`` 表示放行。

        顺序是**从粗到细、从便宜到贵**：全局桶是一个计数器，
        租户桶需要查表。先做便宜的，能在洪峰时省下大量查表开销。
        """
        if not self.edge.try_acquire(now_ms):
            return RejectReason.TOO_MANY_REQUESTS

        bucket = self._tenant_bucket(req.tenant_id)
        if bucket is not None and not bucket.try_acquire(now_ms):
            return RejectReason.TENANT_QUOTA_EXCEEDED

        return None

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "edge_tokens": round(self.edge.tokens, 1),
            "edge_saturation": round(self.edge.saturation, 3),
            "edge_rejected": self.edge.rejected,
            "tenants": {
                tenant: {
                    "tokens": round(bucket.tokens, 1),
                    "rejected": bucket.rejected,
                }
                for tenant, bucket in self._tenant_buckets.items()
            },
        }
