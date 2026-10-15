"""分层缓存 —— L1 / L2 / 语义（说明书 §10）。

三层的差别不只是「快慢」，更重要的是**命中率的上限不同**（§10.1）：

- L1（进程内）：命中延迟 ~0.1ms，但每个实例一份，不一致窗口长；
- L2（共享）：全局一致，是真正决定「上游减负多少」的那一层；
- 语义：**唯一能提高命中率上限的一层**。精确缓存的上限是
  「完全相同请求的占比」，语义缓存的上限是「意图相同请求的占比」。

本 Demo 把「语义命中」做成一个**确定性的事实**而不是一个概率参数：
同一个 ``semantic_key`` 下，某个变体被缓存之后，其他变体再来时就能命中。
这样命中率是由流量画像（``variant_ratio``）**算出来的**，而不是假设出来的 ——
如果它只是一个可调的百分比，这个 Demo 就什么都没证明。
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from ..domain.enums import ServedBy
from ..domain.models import GatewayConfig, Request
from ..sim.rng import Rng


@dataclass
class CacheEntry:
    key: str
    semantic_key: str
    inserted_ms: int
    expires_ms: int
    hits: int = 0
    #: 命中的缓存由哪个变体产生（用于展示「语义命中」的来源）
    origin_variant: int = 0


class _LruTier:
    """带 TTL 的 LRU 层。"""

    def __init__(self, capacity: int, name: str) -> None:
        self.capacity = capacity
        self.name = name
        self._items: OrderedDict[str, CacheEntry] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.inserts = 0
        self.evictions = 0
        self.expired = 0

    def get(self, key: str, now_ms: int) -> CacheEntry | None:
        entry = self._items.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_ms <= now_ms:
            # 惰性过期：不做后台清理线程，读的时候才发现过期
            del self._items[key]
            self.expired += 1
            self.misses += 1
            return None
        self._items.move_to_end(key)
        entry.hits += 1
        self.hits += 1
        return entry

    def put(self, entry: CacheEntry) -> None:
        if self.capacity <= 0:
            return
        if entry.key in self._items:
            self._items.move_to_end(entry.key)
            self._items[entry.key] = entry
            return
        while len(self._items) >= self.capacity:
            self._items.popitem(last=False)
            self.evictions += 1
        self._items[entry.key] = entry
        self.inserts += 1

    def invalidate(self, key: str) -> None:
        self._items.pop(key, None)

    def __len__(self) -> int:
        return len(self._items)

    def snapshot(self) -> dict:
        total = self.hits + self.misses
        return {
            "capacity": self.capacity,
            "size": len(self._items),
            "hits": self.hits,
            "misses": self.misses,
            "hit_ratio": round(self.hits / total, 4) if total else 0.0,
            "evictions": self.evictions,
            "expired": self.expired,
        }


class TieredCache:
    """L1 → L2 → 语义的分层缓存。"""

    def __init__(self, config: GatewayConfig, rng: Rng) -> None:
        self.config = config
        self.rng = rng
        self.l1 = _LruTier(config.l1_capacity, "L1")
        self.l2 = _LruTier(config.l2_capacity, "L2")
        #: 语义索引：semantic_key → 该意图下最近一次被缓存的精确 key
        self._semantic: dict[str, str] = {}
        self.semantic_hits = 0
        self.semantic_stores = 0
        #: §10.2 负缓存：把失败也短暂缓存，防止「同一批相同请求集体重试」
        self._negative: dict[str, int] = {}
        self.negative_hits = 0

    # ------------------------------------------------------------------
    def lookup(self, req: Request, now_ms: int) -> ServedBy | None:
        """从便宜到贵依次查。命中即返回 ``ServedBy``。"""
        entry = self.l1.get(req.cache_key, now_ms)
        if entry is not None:
            return ServedBy.CACHE_L1

        entry = self.l2.get(req.cache_key, now_ms)
        if entry is not None:
            # L1 未命中但 L2 命中 —— 顺手回填 L1（否则同一实例会重复查 L2）
            self._backfill_l1(req, entry, now_ms)
            return ServedBy.CACHE_L2

        if not self.config.semantic_cache_enabled:
            return None

        # §10.1 语义层：同一个意图的**另一个表面形式**被缓存过
        representative = self._semantic.get(req.semantic_key)
        if representative and representative != req.cache_key:
            entry = self.l1.get(representative, now_ms) or self.l2.get(representative, now_ms)
            if entry is not None:
                self.semantic_hits += 1
                return ServedBy.CACHE_SEMANTIC
            # 代表条目已经过期或被淘汰，索引也要清掉，否则会一直空指针
            self._semantic.pop(req.semantic_key, None)
        return None

    def negative_hit(self, req: Request, now_ms: int) -> bool:
        """§10.2 负缓存：已知这一批请求都会失败，就别再打上游了。"""
        until = self._negative.get(req.cache_key)
        if until is None:
            return False
        if until <= now_ms:
            del self._negative[req.cache_key]
            return False
        self.negative_hits += 1
        return True

    # ------------------------------------------------------------------
    def store(self, req: Request, now_ms: int) -> None:
        ttl = self._ttl_ms()
        entry = CacheEntry(
            key=req.cache_key,
            semantic_key=req.semantic_key,
            inserted_ms=now_ms,
            expires_ms=now_ms + ttl,
        )
        self.l1.put(entry)
        self.l2.put(entry)
        if self.config.semantic_cache_enabled:
            self._semantic[req.semantic_key] = req.cache_key
            self.semantic_stores += 1

    def store_negative(self, req: Request, now_ms: int) -> None:
        self._negative[req.cache_key] = now_ms + self.config.cache_ttl_ms // 20

    def _backfill_l1(self, req: Request, entry: CacheEntry, now_ms: int) -> None:
        self.l1.put(
            CacheEntry(
                key=entry.key,
                semantic_key=entry.semantic_key,
                inserted_ms=entry.inserted_ms,
                expires_ms=entry.expires_ms,
                origin_variant=entry.origin_variant,
            )
        )

    def _ttl_ms(self) -> int:
        """§10.2 TTL 抖动。

        **必须加抖动。** 固定 TTL 会让一批同时写入的键在同一个时刻集体过期，
        于是「缓存雪崩」变成了一个**周期性**事件 —— 在监控上会看到
        非常规律的尖峰。加 ±25% 的抖动就能把它摊平。
        """
        base = self.config.cache_ttl_ms
        jitter = self.config.cache_ttl_jitter
        if jitter <= 0:
            return base
        return max(1, int(base * self.rng.uniform(1.0 - jitter, 1.0 + jitter)))

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.config.l1_capacity > 0 or self.config.l2_capacity > 0

    def snapshot(self) -> dict:
        return {
            "l1": self.l1.snapshot(),
            "l2": self.l2.snapshot(),
            "semantic_hits": self.semantic_hits,
            "semantic_stores": self.semantic_stores,
            "negative_hits": self.negative_hits,
        }
