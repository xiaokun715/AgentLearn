"""Metrics —— 必做指标（设计说明书 §46-47）。

重点关注 Queue Wait 与 Execution 必须分开统计：
    Job Total Latency = Queue Wait + Execution

（只看 Agent Execution 会把「队列拥塞」误判为「Agent 很慢」。）
"""
from __future__ import annotations

import threading
import time

# 设计说明书 §46 的必做指标清单
COUNTERS = [
    "agent_jobs_created_total",
    "agent_jobs_completed_total",
    "agent_jobs_failed_total",
    "agent_jobs_cancelled_total",
    "agent_jobs_retried_total",
    "agent_jobs_dead_total",
    "agent_jobs_requeued_total",
    "agent_checkpoint_total",
    "agent_checkpoint_failure_total",
    "agent_jobs_recovered_total",
]

HISTOGRAMS = {
    # name -> buckets（秒）
    "agent_job_duration_seconds": [0.01, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300],
    "agent_job_queue_wait_seconds": [0.01, 0.1, 0.5, 1, 2, 5, 10, 30, 60],
    "agent_job_execution_seconds": [0.01, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300],
}

GAUGES = [
    "agent_queue_depth",
    "agent_worker_active",
]


class Metrics:
    """轻量、无外部依赖的指标聚合器，GET /metrics 输出 Prometheus 文本格式。"""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {n: 0 for n in COUNTERS}
        self._gauges: dict[str, int] = {n: 0 for n in GAUGES}
        # histograms: name -> {"buckets": [...], "counts": [...], "sum": float, "count": int}
        self._hist: dict[str, dict] = {}
        for name, buckets in HISTOGRAMS.items():
            self._hist[name] = {
                "buckets": list(sorted(buckets)),
                "counts": [0] * (len(buckets) + 1),
                "sum": 0.0,
                "count": 0,
            }
        self._lock = threading.Lock()

    # ---- 更新 -------------------------------------------------------------

    def inc(self, name: str, value: int = 1) -> None:
        if name in self._counters:
            with self._lock:
                self._counters[name] += value
        elif name in self._gauges:
            with self._lock:
                self._gauges[name] += value

    def set_gauge(self, name: str, value: int) -> None:
        with self._lock:
            self._gauges[name] = value

    def observe(self, name: str, value: float) -> None:
        """记录一次耗时样本（写入直方图）。"""
        h = self._hist.get(name)
        if h is None:
            return
        with self._lock:
            h["count"] += 1
            h["sum"] += value
            # 落在哪个桶：value <= bucket 的最小桶
            idx = len(h["buckets"])
            for i, b in enumerate(h["buckets"]):
                if value <= b:
                    idx = i
                    break
            h["counts"][idx] += 1

    def timeit(self, name: str) -> "Timer":
        return Timer(self, name)

    # ---- 读取 -------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {
                    n: {"sum": h["sum"], "count": h["count"], "buckets": list(h["buckets"]), "counts": list(h["counts"])}
                    for n, h in self._hist.items()
                },
            }

    def render_prometheus(self) -> str:
        snap = self.snapshot()
        lines: list[str] = []
        for name, value in snap["counters"].items():
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")
        for name, value in snap["gauges"].items():
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        for name, h in snap["histograms"].items():
            lines.append(f"# TYPE {name} histogram")
            lines.append(f'{name}_sum {h["sum"]}')
            lines.append(f'{name}_count {h["count"]}')
            prev = 0.0
            for i, b in enumerate(h["buckets"]):
                lines.append(f'{name}_bucket{{le="{b}"}} {h["counts"][i] + prev}')
                prev += h["counts"][i]
            lines.append(f'{name}_bucket{{le="+Inf"}} {h["count"]}')
        return "\n".join(lines)


class Timer:
    """上下文管理器：记录一段耗时时长。"""

    def __init__(self, metrics: Metrics, name: str) -> None:
        self._metrics = metrics
        self._name = name
        self._start = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc) -> None:
        self._metrics.observe(self._name, time.monotonic() - self._start)
        return False
