"""指标采集（说明书 §16）。

这里有一个刻意的设计：**分位数来自直方图，而不是来自样本列表的排序。**

原因是 §16.2 的那条规则 ——「永远不要把多个实例的 P99 平均」。要能在
多实例场景下正确聚合，上报的就必须是**直方图**（bucket 计数），
在查询时再把 bucket 相加后求分位。如果用「保存所有样本再排序」，
聚合时除了把样本倒出来重排以外没有别的办法，而那在生产上不可行。

所以本模块的 :class:`Histogram` 从一开始就只保留 bucket 计数。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..domain.enums import Outcome, RejectReason, ServedBy
from ..domain.models import OutcomeRecord

#: 分位数桶的上界（毫秒）。跨度从 1ms 到 60s，覆盖「缓存命中」到「灾难」。
#: 1.5s~5s 这一段刻意加密：那是「一次正常的上游调用」到「明显在排队」的分界，
#: 分辨率不够就看不出饱和点在哪里。
BUCKETS_MS: tuple[int, ...] = (
    1, 2, 5, 10, 20, 50, 100, 200, 500,
    1_000, 1_500, 2_000, 3_000, 5_000, 8_000,
    12_000, 20_000, 30_000, 60_000,
)


class Histogram:
    """只保留 bucket 计数的直方图（可聚合）。"""

    def __init__(self, buckets: tuple[int, ...] = BUCKETS_MS) -> None:
        self.buckets = buckets
        #: 每个桶的**上界**，除了最后一个溢出桶
        self.counts: list[int] = [0] * (len(buckets) + 1)
        self.count = 0
        self.total = 0
        self.max_value = 0

    def observe(self, value: int) -> None:
        self.count += 1
        self.total += value
        self.max_value = max(self.max_value, value)
        for index, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[index] += 1
                return
        self.counts[-1] += 1

    def percentile(self, p: float) -> int:
        """返回**桶上界**（保守估计）。

        刻意不做桶内插值：插值会让数字看着更精确，但那个精确是假的 ——
        真实的桶内分布我们并不知道。宁可报一个上界。
        """
        if self.count == 0:
            return 0
        target = p * self.count
        cumulative = 0
        for index, bound in enumerate(self.buckets):
            cumulative += self.counts[index]
            if cumulative >= target:
                return bound
        return self.max_value

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    def snapshot(self) -> dict:
        return {
            "count": self.count,
            "p50": self.percentile(0.50),
            "p90": self.percentile(0.90),
            "p99": self.percentile(0.99),
            "mean": round(self.mean, 1),
            "max": self.max_value,
        }


@dataclass
class TenantStats:
    """§8.3 公平性只能通过**分租户**的分位数看出来。

    全局 P99 在 FIFO 和 WFQ 两种策略下可能差不多 —— 因为大租户的请求
    占了绝大多数，它把整体分位数拉住了。要看公平性，必须按租户拆。
    """

    tenant_id: str
    arrived: int = 0
    ok: int = 0
    rejected: int = 0
    timeout: int = 0
    failed: int = 0
    latency: Histogram = field(default_factory=Histogram)
    queue_wait: Histogram = field(default_factory=Histogram)

    def snapshot(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "arrived": self.arrived,
            "ok": self.ok,
            "rejected": self.rejected,
            "timeout": self.timeout,
            "failed": self.failed,
            "latency": self.latency.snapshot(),
            "queue_wait": self.queue_wait.snapshot(),
        }


class Metrics:
    """一次仿真的全部观测数据。"""

    def __init__(self, sample_interval_ms: int = 100) -> None:
        self.sample_interval_ms = sample_interval_ms
        self.outcomes: list[OutcomeRecord] = []
        self.latency = Histogram()
        self.queue_wait = Histogram()
        self.by_status: dict[Outcome, int] = defaultdict(int)
        self.by_reason: dict[RejectReason, int] = defaultdict(int)
        self.by_served: dict[ServedBy, int] = defaultdict(int)
        self.tenants: dict[str, TenantStats] = {}

        # ---- 时间序列（每个采样间隔一个点）------------------------------
        #: 真正提交给上游的调用次数（含重试）。
        #
        # 这个数**不能**从 outcomes 里统计 —— 重试复用同一个 request_id，
        # 最终只留一条终态记录，于是「重试打出去的额外调用」在结果列表里
        # 完全看不见（实测：朴素重试 67 次，却统计出放大比 1.00）。
        # 放大比是 §12.1 雪崩的核心指标，统计口径错了整个场景就没意义了。
        self.upstream_submissions = 0
        #: 其中由**重试**产生的提交次数
        self.retry_submissions = 0
        self.timeline: dict[str, list[float]] = defaultdict(list)
        self._last_rejected = 0
        self._last_timeout = 0
        self.samples = 0

    # ------------------------------------------------------------------
    def record(self, record: OutcomeRecord) -> None:
        self.outcomes.append(record)
        self.latency.observe(record.latency_ms)
        self.queue_wait.observe(record.queue_ms)
        self.by_status[record.status] += 1
        self.by_served[record.served_by] += 1
        if record.reason is not None:
            self.by_reason[record.reason] += 1

        stats = self.tenants.get(record.tenant_id)
        if stats is None:
            stats = TenantStats(tenant_id=record.tenant_id)
            self.tenants[record.tenant_id] = stats
        stats.arrived += 1
        stats.latency.observe(record.latency_ms)
        stats.queue_wait.observe(record.queue_ms)
        if record.status in (Outcome.OK, Outcome.DEGRADED):
            stats.ok += 1
        elif record.status is Outcome.REJECTED:
            stats.rejected += 1
        elif record.status is Outcome.TIMEOUT:
            stats.timeout += 1
        else:
            stats.failed += 1

    # ------------------------------------------------------------------
    def sample(
        self,
        now_ms: int,
        *,
        queue_depth: int,
        inflight: int,
        limit: int,
        shed_level: str,
        upstream_inflight: int,
        upstream_queue: int,
        pressure: float = 0.0,
    ) -> None:
        """按固定间隔采一个点。

        **`queue_wait_ms` 和 `service_ms` 必须分开采样**（§16.3）：
        只看总延迟，你分不清是上游变慢了还是自己排队了，
        而这两者的处置方式完全不同。所以这里单独记一个
        「队列等待 / 服务时间」的比值 —— 它是雪崩前最有价值的信号。
        """
        self.samples += 1
        rejected = self.by_status[Outcome.REJECTED]
        timed_out = self.by_status[Outcome.TIMEOUT]

        self.timeline["t_s"].append(now_ms / 1000.0)
        self.timeline["queue_depth"].append(float(queue_depth))
        self.timeline["inflight"].append(float(inflight))
        self.timeline["utilisation"].append(inflight / limit if limit else 1.0)
        self.timeline["pressure"].append(round(pressure, 4))
        self.timeline["upstream_queue"].append(float(upstream_queue))
        self.timeline["upstream_inflight"].append(float(upstream_inflight))
        # 每个采样间隔内的**新增**拒绝数 → 拒绝率（累计值画出来是一条斜线，看不出突变）
        self.timeline["rejected_per_sample"].append(float(rejected - self._last_rejected))
        self.timeline["timeout_per_sample"].append(float(timed_out - self._last_timeout))
        self.timeline["shed_level"].append(
            {"NORMAL": 0.0, "SOFT": 1.0, "HARD": 2.0, "CRITICAL": 3.0}.get(shed_level, 0.0)
        )
        self._last_rejected = rejected
        self._last_timeout = timed_out

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def succeeded(self) -> int:
        return self.by_status[Outcome.OK] + self.by_status[Outcome.DEGRADED]

    @property
    def rejected(self) -> int:
        return self.by_status[Outcome.REJECTED]

    @property
    def timed_out(self) -> int:
        return self.by_status[Outcome.TIMEOUT]

    @property
    def failed(self) -> int:
        return self.by_status[Outcome.FAILED]

    @property
    def degraded(self) -> int:
        return self.by_status[Outcome.DEGRADED]

    @property
    def upstream_calls(self) -> int:
        """真正打到上游的调用数（含重试）—— §12.1 负载放大的分子。"""
        return self.upstream_submissions

    def served_by(self, which: ServedBy) -> int:
        return self.by_served.get(which, 0)

    @property
    def retry_amplification(self) -> float:
        """**重试放大比** = 上游提交数 / 首次提交数。

        它只衡量「重试把负载放大了多少」，与缓存、合并、拒绝统统无关。
        这一点必须分清楚：早先版本把它算成「上游调用 / 请求总数」，
        结果缓存命中会让这个数**低于 1**，看起来像「重试把负载减小了」——
        一个指标同时承载两件事，就两件事都读不出来。

        §12.1：失败率 80%、重试 3 次时它会达到 2.44，那就是雪崩的燃料。
        """
        first = self.upstream_submissions - self.retry_submissions
        return self.upstream_submissions / first if first > 0 else 0.0

    @property
    def upstream_ratio(self) -> float:
        """**上行占比** = 上游提交数 / 请求总数。它是「减负了多少」的度量。"""
        return self.upstream_submissions / self.total if self.total else 0.0

    def summary(self, duration_ms: int) -> dict:
        seconds = duration_ms / 1000.0
        return {
            "requests": self.total,
            # 两个吞吐要分开：offered 是「被要求做多少」，achieved 是
            # 「真正做成了多少」。过载时这两个数会差一倍以上，
            # 用 offered 冒充 achieved 是最常见的一种压测自欺。
            "offered_rps": round(self.total / seconds, 2),
            "achieved_rps": round(self.succeeded / seconds, 2),
            "succeeded": self.succeeded,
            "degraded": self.degraded,
            "rejected": self.rejected,
            "timed_out": self.timed_out,
            "failed": self.failed,
            "success_ratio": round(self.succeeded / self.total, 4) if self.total else 0.0,
            "latency": self.latency.snapshot(),
            "queue_wait": self.queue_wait.snapshot(),
            "upstream_calls": self.upstream_calls,
            "retry_amplification": round(self.retry_amplification, 3),
            "upstream_ratio": round(self.upstream_ratio, 4),
            "served_by": {k.value: v for k, v in self.by_served.items()},
            "reject_reasons": {k.value: v for k, v in self.by_reason.items()},
            "cache_offload": round(
                (
                    self.served_by(ServedBy.CACHE_L1)
                    + self.served_by(ServedBy.CACHE_L2)
                    + self.served_by(ServedBy.CACHE_SEMANTIC)
                    + self.served_by(ServedBy.COALESCED)
                ) / self.total, 4,
            ) if self.total else 0.0,
        }

    def tenant_summary(self) -> list[dict]:
        return [
            stats.snapshot()
            for stats in sorted(self.tenants.values(), key=lambda s: s.tenant_id)
        ]
