"""网关管线 —— 说明书 §5 的 11 个阶段。

阶段顺序不是随意的，每一处都有理由：

    ① 超时预算      最先算 —— 后面每一步都要用它判断「还值不值得做」
    ② 边缘限流      最便宜的拒绝，保护的是**自己**
    ③ 租户配额      放在缓存之前：配额是「资格」问题，不该让超配额租户蹭缓存
    ④ 熔断检查      上游已经挂了就别排队了
    ⑤ 缓存查询      命中能免掉后面所有环节，且必须最便宜的先查
    ⑥ 请求合并      搭车不占队列，所以必须在准入**之前**
    ⑦ 准入控制      排队前的守门人，判据是「预计完成时间」而非「队列长度」
    ⑧ 入队（有界）
    ⑨ 调度 + 并发信号量
    ⑩ 提交上游
    ⑪ 完成 / 拒绝 / 超时 / 降级

**一条贯穿全文件的不变量**（合并最容易在这里出错）：

    任何一个 leader 请求走到终态时，都必须把它的等待者一并终结。

漏掉任何一条路径，等待者就会永远挂在 singleflight 上 ——
这是 singleflight 实现里最常见的 bug，所以这里把它收敛到
:meth:`Gateway._finish` 一个出口，而不是散落在十几个分支里。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..domain.enums import (
    Outcome,
    Priority,
    RejectReason,
    ServedBy,
)
from ..domain.models import GatewayConfig, Request
from ..sim.rng import Rng
from ..sim.upstream import UpstreamCall, UpstreamLLM
from .admission import AdmissionController, ThroughputEstimator
from .cache import TieredCache
from .coalescer import Coalescer
from .limiter import RateLimiterSet
from .protect import CircuitBreaker, RetryBudget, RetryPlanner, UpstreamSemaphore
from .scheduler import BoundedScheduler

#: 结果出口：每产生一个终态就调用一次
OutcomeSink = Callable[["object"], None]


@dataclass
class InFlight:
    """一次正在上游执行的调用。"""

    call_id: str
    req: Request
    started_ms: int
    degrade: bool = False
    #: 搭在这趟车上的等待者（§9.1），完成时一并扇出
    riders: list[Request] = field(default_factory=list)


@dataclass
class GatewayCounters:
    """网关自身的计数 —— 每一项都对应 §16.1 的一个观测需求。"""

    arrived: int = 0
    cache_hit_l1: int = 0
    cache_hit_l2: int = 0
    cache_hit_semantic: int = 0
    coalesced: int = 0
    enqueued: int = 0
    dispatched: int = 0
    upstream_calls: int = 0
    upstream_429: int = 0
    retries: int = 0
    rejected: int = 0
    queue_full: int = 0
    #: 被**周期性清扫**移出队列的（只有 sweep_expired=True 时才会发生）
    swept_timeout: int = 0
    #: 出队那一刻才发现的超期请求。这一步**不依赖清扫开关** ——
    #: 它是最后一道闸门，必须在派发前再确认一次。
    #: 把两者混在一个计数器里，就分不清「清扫到底有没有用」了。
    dispatch_expired: int = 0
    inflight_timeout: int = 0
    inflight_cancelled: int = 0
    completed_ok: int = 0
    completed_degraded: int = 0
    failed: int = 0


class Gateway:
    """流量治理网关。"""

    def __init__(
        self,
        config: GatewayConfig,
        upstream: UpstreamLLM,
        rng: Rng,
        sink: OutcomeSink,
    ) -> None:
        self.config = config
        self.upstream = upstream
        self.rng = rng
        self.sink = sink
        self.counters = GatewayCounters()

        self.limiters = RateLimiterSet(config)
        self.cache = TieredCache(config, rng)
        self.coalescer = Coalescer()
        self.scheduler = BoundedScheduler(config)
        self.semaphore = UpstreamSemaphore(config.upstream_concurrency)
        self.breaker = CircuitBreaker(config)
        self.retry_budget = RetryBudget(config.retry_budget_ratio)
        self.retry_planner = RetryPlanner(config)
        self.estimator = ThroughputEstimator(
            window_ms=config.throughput_window_ms,
            static_capacity_per_ms=(
                config.upstream_concurrency / max(config.service_time_estimate_ms, 1)
            ),
        )
        self.admission = AdmissionController(
            config, self.estimator, config.upstream_concurrency
        )

        self._inflight: dict[str, InFlight] = {}
        #: request_id → cache_key，仅对 leader 有值。终态时必须据此释放等待者。
        self._leader_key: dict[str, str] = {}
        self._enqueued_at: dict[str, int] = {}
        #: 派发时就固定下来的「排队等待」。**不能等到完成时再算** ——
        #: 那样算出来的是「总延迟」，而 §16.3 要求把排队和计算分开：
        #: 只看总延迟，你分不清是上游变慢了还是自己排队了。
        self._queue_wait_ms: dict[str, int] = {}
        self._last_sweep_ms = 0
        self._call_seq = 0
        #: 每个采样点的在途/队列，供 §16 的时间序列使用
        self.timeline: dict[str, list[float]] = {
            "queue_depth": [], "inflight": [], "utilisation": [],
            "rejected": [], "shed_level": [], "pressure": [],
        }

    # ==================================================================
    # ①~⑧ 入口
    # ==================================================================
    def handle(self, req: Request, now_ms: int) -> None:
        self.counters.arrived += 1

        # ---- ②③ 多级限流（§6）----------------------------------------
        reason = self.limiters.check(req, now_ms)
        if reason is not None:
            self._reject(req, reason, now_ms, "限流：配额/洪峰")
            return

        # ---- ④ 熔断检查（§12.4）---------------------------------------
        if not self.breaker.allow(now_ms):
            self._reject(
                req, RejectReason.CIRCUIT_OPEN, now_ms,
                f"上游熔断中（失败率 {self.breaker.failure_ratio:.0%}）",
            )
            return

        # ---- ⑤ 缓存查询（§10）-----------------------------------------
        if self.cache.enabled:
            served_by = self.cache.lookup(req, now_ms)
            if served_by is not None:
                self._hit_cache(req, served_by, now_ms)
                return
            # §10.2 负缓存：这一批相同请求已知都会失败，别再打上游
            if self.cache.negative_hit(req, now_ms):
                self._finish(
                    req, now_ms, status=Outcome.FAILED,
                    reason=RejectReason.UPSTREAM_ERROR,
                    served_by=ServedBy.CACHE_L2,
                    note="负缓存命中（近期同 key 已连续失败）",
                )
                return

        # ---- ⑥ 请求合并（§9）------------------------------------------
        if self.config.coalescing_enabled and self.coalescer.join(req, now_ms):
            # 搭车成功：**不走准入，也不入队** —— 因为搭车不消耗队列位置。
            # 它的终态在 leader 完成时统一扇出（见 _finish 的约定）。
            self.counters.coalesced += 1
            return

        # ---- ⑦ 准入控制（§7）------------------------------------------
        decision = self.admission.decide(
            req, now_ms,
            queue_depth=self.scheduler.depth,
            inflight=self.semaphore.inflight,
        )
        if not decision.allow:
            self._reject(req, decision.reason, now_ms, decision.rationale)
            return
        # 把降级标记写回请求，让它跟着穿过队列
        if decision.degrade:
            req = req.model_copy(update={"degrade": True})

        # ---- ⑧ 入队（有界，§8.1）---------------------------------------
        if not self.scheduler.enqueue(req, now_ms):
            self.counters.queue_full += 1
            self._reject(
                req, RejectReason.QUEUE_FULL, now_ms,
                f"有界队列已满（{self.scheduler.depth}/{self.scheduler.capacity}）",
            )
            return

        self.counters.enqueued += 1
        self._enqueued_at[req.request_id] = now_ms
        # 成为 leader：登记一次飞行，后续同 key 的请求可以搭车
        if self.config.coalescing_enabled and req.coalescable:
            self.coalescer.start(req, now_ms)
            self._leader_key[req.request_id] = req.cache_key

    # ==================================================================
    # ⑨⑩ 调度与派发
    # ==================================================================
    def tick(self, now_ms: int) -> None:
        self._maybe_sweep(now_ms)
        self._dispatch(now_ms)

    def _maybe_sweep(self, now_ms: int) -> None:
        """§11.2 超期清扫：把注定失败的工作从队列里移出去。

        这是「端到端超时预算」相对「每层各设超时」最大的额外收益：
        释放出来的槽位立刻会被后面的请求用上。
        """
        if not self.config.sweep_expired:
            return
        if now_ms - self._last_sweep_ms < self.config.sweep_interval_ms:
            return
        self._last_sweep_ms = now_ms
        for req in self.scheduler.sweep_expired(now_ms):
            self.counters.swept_timeout += 1
            self._finish(
                req, now_ms, status=Outcome.TIMEOUT, served_by=ServedBy.UPSTREAM,
                note="排队期间已超过 deadline，被清扫出队",
            )

    def _dispatch(self, now_ms: int) -> None:
        """从队列取出请求填满并发槽位。"""
        while self.semaphore.inflight < self.semaphore.limit:
            req = self.scheduler.pop(now_ms)
            if req is None:
                break
            # 出队时再确认一次 deadline：从入队到现在可能已经过了很久，
            # 而且清扫是周期性的（默认 50ms），窗口内的请求会漏网。
            if req.deadline_ms <= now_ms:
                self.counters.dispatch_expired += 1
                self._finish(
                    req, now_ms, status=Outcome.TIMEOUT,
                    note="出队时已超过 deadline",
                )
                continue
            self._submit(req, now_ms)

    def _submit(self, req: Request, now_ms: int) -> None:
        self.semaphore.try_acquire()
        self.counters.dispatched += 1
        enqueued_at = self._enqueued_at.pop(req.request_id, None)
        self._queue_wait_ms[req.request_id] = (
            now_ms - enqueued_at if enqueued_at is not None else 0
        )
        self._call_seq += 1

        # §13.2 brownout：降级用更小的模型 —— 服务时间更短，质量更差。
        # 标记取自**准入那一刻**的判断（它跟着请求穿过队列），而不是现在现算一个：
        # 现算的话，一个在过载时被批准降级的请求，排到队首时系统可能已经恢复正常，
        # 于是它既被降级了、又「本不该被降级」—— 观测数据就自相矛盾了。
        degrade = req.degrade and self.config.degrade_enabled

        call = UpstreamCall(
            call_id=f"call_{self._call_seq}",
            request_id=req.request_id,
            submitted_ms=now_ms,
            degraded=degrade,
            speed_factor=self.config.degrade_service_factor if degrade else 1.0,
        )
        self.counters.upstream_calls += 1

        if not self.upstream.submit(call, now_ms):
            # 上游 429：§3.5「必须假设上游的容量小于你的接入能力」
            self.counters.upstream_429 += 1
            self.semaphore.release()
            self._on_upstream_failure(req, now_ms, reason="upstream_429")
            return

        self._inflight[call.call_id] = InFlight(
            call_id=call.call_id, req=req, started_ms=now_ms, degrade=degrade,
        )

    # ==================================================================
    # ⑪ 上游回调
    # ==================================================================
    def on_upstream_done(self, call: UpstreamCall, ok: bool, now_ms: int) -> None:
        """由上游在完成/失败时回调（引擎已保证顺序确定性）。"""
        flight = self._inflight.pop(call.call_id, None)
        if flight is None:                       # 已经取消过的调用
            return
        self.semaphore.release()
        req = flight.req

        if ok:
            self.breaker.record(True, now_ms)
            self.retry_budget.on_success()
            self.estimator.observe_completion(now_ms)
            status = Outcome.DEGRADED if flight.degrade else Outcome.OK
            if flight.degrade:
                self.counters.completed_degraded += 1
            else:
                self.counters.completed_ok += 1

            if req.deadline_ms <= now_ms:
                # 上游确实返回了，但对调用方而言已经太晚 —— 如实记为 TIMEOUT。
                # 这正是「超时预算」要预防的事：槽位被占用了，结果却没人要。
                self.counters.inflight_timeout += 1
                self._finish(
                    req, now_ms, status=Outcome.TIMEOUT,
                    served_by=ServedBy.UPSTREAM,
                    note="上游返回时已超过 deadline",
                )
                return

            self.cache.store(req, now_ms) if self.cache.enabled else None
            self._finish(
                req, now_ms, status=status, served_by=ServedBy.UPSTREAM,
                degraded=flight.degrade,
            )
            return

        # 失败：交给重试预算裁决（§12.2）
        self.breaker.record(False, now_ms)
        self._on_upstream_failure(req, now_ms, reason="upstream_error")

    def _on_upstream_failure(self, req: Request, now_ms: int, *, reason: str) -> None:
        backoff = self.retry_planner.backoff_ms(self.rng, req.attempts)
        if (
            self.config.retry_enabled
            and self.retry_planner.should_retry(req.attempts, now_ms, req.deadline_ms, backoff)
            and self.retry_budget.allow()
        ):
            # §5 的两个易错点之一：重试要**重新入队**，而且要**降优先级**。
            # 直接原地重试会占着槽位不放，而且在过载时把「已经失败过的请求」
            # 优先于「还没试过的新请求」—— 方向是反的。
            retry = req.model_copy(
                update={
                    "attempts": req.attempts + 1,
                    "is_retry": True,
                    "priority": Priority(max(0, req.priority.value - 1)),
                    "arrived_ms": req.arrived_ms,   # deadline 从**原始**到达时间算
                }
            )
            self.counters.retries += 1
            if self.scheduler.enqueue(retry, now_ms):
                self._enqueued_at[retry.request_id] = now_ms
                return
            self._reject(
                retry, RejectReason.QUEUE_FULL, now_ms,
                "重试入队时队列已满",
            )
            return

        self.counters.failed += 1
        if self.cache.enabled:
            self.cache.store_negative(req, now_ms)
        self._finish(
            req, now_ms, status=Outcome.FAILED, reason=RejectReason.UPSTREAM_ERROR,
            served_by=ServedBy.UPSTREAM,
            note=f"上游失败（{reason}）且不再重试",
        )

    # ==================================================================
    # 终态出口 —— 合并的一致性是靠「只有一个出口」保证的
    # ==================================================================
    def _hit_cache(self, req: Request, served_by: ServedBy, now_ms: int) -> None:
        if served_by is ServedBy.CACHE_L1:
            self.counters.cache_hit_l1 += 1
        elif served_by is ServedBy.CACHE_L2:
            self.counters.cache_hit_l2 += 1
        else:
            self.counters.cache_hit_semantic += 1
        self._finish(req, now_ms, status=Outcome.OK, served_by=served_by)

    def _reject(
        self, req: Request, reason: RejectReason | None, now_ms: int, note: str,
    ) -> None:
        self.counters.rejected += 1
        self._finish(
            req, now_ms, status=Outcome.REJECTED, reason=reason,
            served_by=ServedBy.UPSTREAM, note=note,
        )

    def _finish(
        self,
        req: Request,
        now_ms: int,
        *,
        status: Outcome,
        reason: RejectReason | None = None,
        served_by: ServedBy = ServedBy.UPSTREAM,
        degraded: bool = False,
        note: str = "",
    ) -> None:
        """唯一的终态出口。

        它做两件事：记录 leader 的结果，**并终结所有搭在它身上的等待者**。
        漏掉第二条会让等待者永远挂起（singleflight 最常见的 bug），
        所以这里是唯一的出口 —— 新增一条路径时不可能忘记。
        """
        self._emit(
            req, now_ms, status=status, reason=reason,
            served_by=served_by, degraded=degraded,
        )

        key = self._leader_key.pop(req.request_id, None)
        if key is None or not self.config.coalescing_enabled:
            return

        for rider in self.coalescer.complete(key):
            # 搭车者的 served_by 是 COALESCED；如果是失败/拒绝，
            # 它拿到的也是同一个原因 —— 合并的语义是「共享结果」，
            # 而失败的结果也是一种结果。
            rider_served = (
                ServedBy.COALESCED if status in (Outcome.OK, Outcome.DEGRADED) else served_by
            )
            self._emit(
                rider, now_ms,
                status=Outcome.DEGRADED if (degraded and status is Outcome.DEGRADED) else status,
                reason=reason,
                served_by=rider_served,
                degraded=degraded,
            )
        self._inflight.pop(req.request_id, None)

    def _emit(
        self,
        req: Request,
        now_ms: int,
        *,
        status: Outcome,
        reason: RejectReason | None,
        served_by: ServedBy,
        degraded: bool,
    ) -> None:
        from ..domain.models import OutcomeRecord

        self._enqueued_at.pop(req.request_id, None)
        queue_ms = self._queue_wait_ms.pop(req.request_id, 0)
        self.sink(
            OutcomeRecord(
                request_id=req.request_id,
                tenant_id=req.tenant_id,
                priority=req.priority,
                status=status,
                reason=reason,
                served_by=served_by,
                latency_ms=now_ms - req.arrived_ms,
                queue_ms=queue_ms,
                attempts=req.attempts,
                degraded=degraded,
                arrived_ms=req.arrived_ms,
                finished_ms=now_ms,
            )
        )

    # ==================================================================
    # 收尾：把还没终态的请求如实计入统计
    # ==================================================================
    def drain_pending(self, now_ms: int) -> int:
        """压测/仿真窗口结束时，把所有未完成的请求记为 TIMEOUT。

        **不做这一步，压测结果会系统性偏乐观**：还没完成的请求
        （队列里排着的、上游正在跑的）根本不会产生 ``OutcomeRecord``，
        于是它们既不在延迟直方图里，也不在失败率里 —— 而且被漏掉的
        恰好是最慢的那一批。

        这正是 §8.1 说的「无界队列把失败隐藏到最后一刻」的一种表现：
        连**统计**都会被它骗过去。
        """
        pending = 0
        # ① 队列里还剩的（用一个大到不可能有请求满足的 now 强制全部到期）
        for req in self.scheduler.sweep_expired(1 << 62):
            pending += 1
            self._finish(
                req, now_ms, status=Outcome.TIMEOUT,
                note="仿真结束时仍在排队",
            )
        # ② 已经提交给上游、还没回来的
        for flight in list(self._inflight.values()):
            pending += 1
            self._finish(
                flight.req, now_ms, status=Outcome.TIMEOUT,
                note="仿真结束时仍在上游执行",
            )
        self._inflight.clear()
        # ③ 兜底：leader 从未走完任何终态出口时留下的等待者
        for rider in self.coalescer.drain_all():
            pending += 1
            self._emit(
                rider, now_ms, status=Outcome.TIMEOUT, reason=None,
                served_by=ServedBy.UPSTREAM, degraded=False,
            )
        return pending

    # ==================================================================
    # §14 弹性伸缩
    # ==================================================================
    def maybe_autoscale(self, now_ms: int, *, desired_limit: int | None = None) -> None:
        """由引擎按扩容指标算出 ``desired_limit`` 后调用。"""
        if desired_limit is not None and desired_limit != self.semaphore.limit:
            self.semaphore.set_limit(desired_limit)

    # ==================================================================
    def current_shed_level(self) -> str:
        return self.admission.last_shed_level.value

    def sample(self, now_ms: int, shed_level: str = "NORMAL") -> None:
        self.semaphore.sample()
        self.timeline["queue_depth"].append(float(self.scheduler.depth))
        self.timeline["inflight"].append(float(self.semaphore.inflight))
        self.timeline["utilisation"].append(round(self.semaphore.utilisation, 4))
        self.timeline["rejected"].append(float(self.counters.rejected))
        self.timeline["shed_level"].append(
            {"NORMAL": 0.0, "SOFT": 1.0, "HARD": 2.0, "CRITICAL": 3.0}.get(shed_level, 0.0)
        )

    # ==================================================================
    def snapshot(self) -> dict:
        return {
            "counters": self.counters.__dict__.copy(),
            "limiters": self.limiters.snapshot(),
            "cache": self.cache.snapshot(),
            "coalescer": self.coalescer.snapshot(),
            "scheduler": self.scheduler.snapshot(),
            "semaphore": {
                "limit": self.semaphore.limit,
                "inflight": self.semaphore.inflight,
                "denied": self.semaphore.denied_total,
            },
            "admission": self.admission.snapshot(),
            "retry_budget": self.retry_budget.snapshot(),
            "retry_planner": self.retry_planner.snapshot(),
            "breaker": self.breaker.snapshot(),
            "throughput_estimate_rps": round(
                self.estimator.estimate_per_ms(0) * 1000, 3
            ),
        }
