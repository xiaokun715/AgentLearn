"""请求合并 —— Singleflight（说明书 §9）。

这是 Agent 网关相对普通 API 网关**独有**的一项优化（§3.3）：
100 个用户在同一秒问了同一句话，普通 API 只能算 100 次，
Agent 服务可以**算一次、扇出 100 次**。

它的收益用一个数衡量 —— **合并放大比**：

    放大比 = 实际请求数 / 实际发生的上游调用数

§9.2 列了不能合并的几类请求（带副作用、参数不同、有独立计费语义、
带取消语义）。本模块把这条边界落成 :attr:`~app.domain.models.Request.coalescable`
一个字段，由流量画像决定，而不是由合并器猜测。

**合并必须包在缓存回填的外面**（§10.2）：缓存过期的瞬间，
1000 个请求同时未命中，如果没有 singleflight 把它们收拢成一次上游调用，
那一刻就是一次小型雪崩。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.models import Request


@dataclass
class Flight:
    """一次正在进行的上游调用，以及搭在它上面的所有等待者。"""

    key: str
    leader: Request
    started_ms: int
    waiters: list[Request] = field(default_factory=list)

    @property
    def follower_count(self) -> int:
        return len(self.waiters)


class Coalescer:
    """按 ``cache_key`` 做 singleflight。

    键用**精确指纹**而不是语义指纹：合并比缓存更严格 ——
    缓存命中最多是「结果可能不够精确」，而合并会让两个请求
    **共享同一个响应对象**，一旦键不严格就可能串结果。
    """

    def __init__(self) -> None:
        self._inflight: dict[str, Flight] = {}
        self.flights = 0          # 真正发起的上游调用数（= leader 数）
        self.joined = 0           # 搭车成功的请求数
        self.abandoned = 0

    # ------------------------------------------------------------------
    def join(self, req: Request, now_ms: int) -> bool:
        """尝试搭车。返回 ``True`` 表示**已挂到别人的飞行上**，调用方应停止后续处理。"""
        if not req.coalescable:
            return False
        flight = self._inflight.get(req.cache_key)
        if flight is None:
            return False
        flight.waiters.append(req)
        self.joined += 1
        return True

    def start(self, req: Request, now_ms: int) -> Flight:
        """成为 leader：登记一次飞行。"""
        flight = Flight(key=req.cache_key, leader=req, started_ms=now_ms)
        self._inflight[req.cache_key] = flight
        self.flights += 1
        return flight

    def complete(self, key: str) -> list[Request]:
        """扇出：取出所有等待者并结束这次飞行。"""
        flight = self._inflight.pop(key, None)
        return list(flight.waiters) if flight else []

    def abandon(self, key: str) -> list[Request]:
        """leader 失败且不再重试时，把所有等待者交还给调用方去处理。

        **不能悄悄丢掉它们。** 合并者搭的是「一次成功的车」，
        如果车没到，每个等待者都需要一个明确的失败结果，
        否则它们会永远挂在那里 —— 这是 singleflight 最常见的实现错误。
        """
        flight = self._inflight.pop(key, None)
        if flight is None:
            return []
        self.abandoned += len(flight.waiters)
        return list(flight.waiters)

    def drop_waiters_of(self, req: Request) -> list[Request]:
        """leader 自己放弃时（例如被准入拒绝）释放等待者。"""
        return self.abandon(req.cache_key)

    def drain_all(self) -> list[Request]:
        """压测窗口结束时，把所有还没被扇出的等待者交出来。

        不交出它们就等于**统计上凭空少了一批请求** —— 而且少的恰好是
        最慢的那一批（还没拿到结果的那些），于是压测结果会系统性偏乐观。
        """
        orphans: list[Request] = []
        for flight in self._inflight.values():
            orphans.extend(flight.waiters)
        self._inflight.clear()
        return orphans

    # ------------------------------------------------------------------
    @property
    def inflight_keys(self) -> int:
        return len(self._inflight)

    def snapshot(self) -> dict:
        total = self.flights + self.joined
        return {
            "flights": self.flights,
            "joined": self.joined,
            "in_flight_now": len(self._inflight),
            "amplification": round(total / self.flights, 3) if self.flights else 0.0,
            "abandoned": self.abandoned,
        }
