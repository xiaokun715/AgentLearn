"""有界队列与公平调度（说明书 §8）。

三种策略共用一套「每租户一个队列 + 选谁出队」的结构，区别只在
**出队的判据**：

============================ ==================================================
策略                          出队判据
============================ ==================================================
``FIFO``                      队头到达时间最早 —— §8.3 的公平性陷阱就在这里
``PRIORITY_AGING``            有效优先级最高（基础优先级 + 等待时长 × 老化权重）
``WFQ``                       虚拟开始时间最小（start-time fair queueing）
============================ ==================================================

**为什么要有界**（§8.1）：无界队列不是「更友善」，而是把「立刻失败」
换成了「延迟很久之后仍然失败」—— 而后者的代价更高，因为它在等待期间
一直占着队列位置，而且调用方在等待时无法做别的选择。
"""
from __future__ import annotations

from collections import deque

from ..domain.enums import QueuePolicy
from ..domain.models import GatewayConfig, Request

#: 清扫周期：扫一次是 O(队列长度)，真实系统也不会每毫秒扫一遍。
#: 这个值直接决定「超期请求在队列里多待多久才被释放」（§11.2）。
DEFAULT_SWEEP_INTERVAL_MS = 50


class BoundedScheduler:
    """有界 + 可插拔调度策略的排队层。"""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.capacity = config.queue_capacity
        self.policy = config.queue_policy
        self.aging_per_second = config.aging_per_second
        self._queues: dict[str, deque[Request]] = {}
        self._depth = 0
        #: WFQ 虚拟时间状态（§8.4）
        self._vtime_global: float = 0.0
        self._last_finish: dict[str, float] = {}
        self._start_time: dict[str, float] = {}
        self.enqueued = 0
        self.rejected_full = 0
        self.swept = 0

    # ------------------------------------------------------------------
    @property
    def depth(self) -> int:
        return self._depth

    def tenant_depth(self) -> dict[str, int]:
        return {tid: len(q) for tid, q in self._queues.items() if q}

    def is_full(self) -> bool:
        return self._depth >= self.capacity

    # ------------------------------------------------------------------
    def enqueue(self, req: Request, now_ms: int) -> bool:
        """入队。返回 ``False`` 表示队列已满（§8.1 的快速失败）。"""
        if self.is_full():
            self.rejected_full += 1
            return False

        if self.policy is QueuePolicy.WFQ:
            # §8.4 虚拟时间：每消耗一次服务，虚拟时间前进 1/weight。
            # 权重越大前进越少 → 被选中越频繁。这就是「加权公平」的全部机制。
            last = self._last_finish.get(req.tenant_id, 0.0)
            start = max(last, self._vtime_global)
            self._last_finish[req.tenant_id] = start + 1.0 / max(req.tenant_weight, 1e-6)
            self._start_time[req.request_id] = start

        self._queues.setdefault(req.tenant_id, deque()).append(req)
        self._depth += 1
        self.enqueued += 1
        return True

    # ------------------------------------------------------------------
    def pop(self, now_ms: int) -> Request | None:
        """按策略选出下一个被服务的请求。

        **FIFO 与 WFQ 只看每个租户队列的队头，优先级策略必须扫描全部。**
        这不是优化取舍，是正确性问题：

        · FIFO / WFQ 的排序键在**同一个租户队列内部**是单调的
          （按入队顺序 / 按虚拟时间），所以全局最优一定在某个队头里；
        · 优先级不单调 —— 同一个租户后进来的高优先级请求会排在
          先到达的低优先级请求后面。只看队头的话，**租户内部的优先级
          完全失效**（实测：一个 batch 请求会抢在一个 interactive 之前被服务）。
        """
        if self.depth == 0:
            return None

        if self.policy is QueuePolicy.PRIORITY_AGING:
            best: tuple[str, Request] | None = None
            best_key: tuple[float, int, str] | None = None
            for tenant_id, queue in self._queues.items():
                for req in queue:
                    # 取负号是因为要「最大者胜」，而我们要找的是最小值。
                    key = (
                        -req.effective_priority(now_ms, self.aging_per_second),
                        req.arrived_ms,
                        req.request_id,
                    )
                    if best_key is None or key < best_key:
                        best_key, best = key, (tenant_id, req)
        else:
            heads = [
                (tenant_id, queue[0])
                for tenant_id, queue in self._queues.items()
                if queue
            ]
            if self.policy is QueuePolicy.FIFO:
                # 队头到达时间最早者胜 —— 就这一行构成了 §8.3 的公平性陷阱：
                # 大租户把队列填满时，小租户的请求要等**一整队**。
                best = min(heads, key=lambda item: (item[1].arrived_ms, item[1].request_id))
            else:  # WFQ
                best = min(
                    heads,
                    key=lambda item: (
                        self._start_time.get(item[1].request_id, 0.0),
                        item[1].arrived_ms,
                        item[1].request_id,
                    ),
                )

        if best is None:
            return None
        tenant_id, req = best

        if self.policy is QueuePolicy.WFQ:
            self._vtime_global = max(
                self._vtime_global, self._start_time.get(req.request_id, 0.0)
            )

        self._queues[tenant_id].remove(req)
        self._depth -= 1
        self._start_time.pop(req.request_id, None)
        return req

    # ------------------------------------------------------------------
    def sweep_expired(self, now_ms: int) -> list[Request]:
        """§11.2 清扫已经超过 deadline 的排队请求。

        这是「超时预算」相对于「每层各设超时」最大的一个额外收益：
        已经不可能完成的请求被**主动移出队列**，而不是继续占着位置
        等到上游再失败一次。

        实测（``deadline`` 场景）能释放约 18% 的算力 —— 这 18% 直接
        转化为有效吞吐，因为槽位会立刻被后面的请求用上。
        """
        expired: list[Request] = []
        for tenant_id, queue in self._queues.items():
            if not queue:
                continue
            keep: deque[Request] = deque()
            for req in queue:
                if req.deadline_ms <= now_ms:
                    expired.append(req)
                    self._start_time.pop(req.request_id, None)
                    self._depth -= 1
                else:
                    keep.append(req)
            self._queues[tenant_id] = keep
        self.swept += len(expired)
        return expired

    def remove(self, req: Request) -> bool:
        queue = self._queues.get(req.tenant_id)
        if not queue:
            return False
        try:
            queue.remove(req)
        except ValueError:
            return False
        self._depth -= 1
        self._start_time.pop(req.request_id, None)
        return True

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "policy": self.policy.value,
            "depth": self._depth,
            "capacity": self.capacity,
            "by_tenant": self.tenant_depth(),
        }
