"""轻量 Metrics（设计说明书 §40~§41）。

Prometheus 文本格式输出，进程内计数器：

    webhook_events_total
    webhook_deliveries_total
    webhook_delivery_success_total
    webhook_delivery_failed_total
    webhook_delivery_retry_total
    webhook_dlq_total
    webhook_delivery_attempts
    webhook_delivery_latency_seconds   (sum + count，可算 P95)

核心指标（§41）：Delivery Lag = delivery_success_time - event_created_time，
这里记录为 delivery 处理的 latency；完整 Lag 需要跨端时间戳，Demo 用 worker
处理耗时近似。
"""
from __future__ import annotations

import statistics
import threading
import time
from collections import deque


class Metrics:
    """线程安全计数器（单进程 Demo 足够；生产应接 Prometheus SDK）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events_total = 0
        self.deliveries_total = 0
        self.delivery_success = 0
        self.delivery_failed = 0
        self.delivery_retry = 0
        self.delivery_dlq = 0
        self.attempts_total = 0
        self._latency_samples: deque[float] = deque(maxlen=1000)  # 秒

    # ---- 钩子 ----------------------------------------------------------------
    def on_event_created(self) -> None:
        with self._lock:
            self.events_total += 1

    def on_delivery_created(self) -> None:
        with self._lock:
            self.deliveries_total += 1

    def on_attempt(self, started: float) -> None:
        with self._lock:
            self.attempts_total += 1
            self._latency_samples.append(time.perf_counter() - started)

    def on_result(self, *, success: bool, retried: bool, dlq: bool) -> None:
        with self._lock:
            if success:
                self.delivery_success += 1
            elif dlq:
                self.delivery_dlq += 1
            elif retried:
                self.delivery_retry += 1
            else:
                self.delivery_failed += 1

    # ---- 计算 ----------------------------------------------------------------
    def p95_latency_ms(self) -> float | None:
        with self._lock:
            if not self._latency_samples:
                return None
            return round(statistics.quantiles(list(self._latency_samples), n=20)[18] * 1000, 2)

    def render(self) -> str:
        """输出 Prometheus 文本格式。"""
        with self._lock:
            latency_sum = sum(self._latency_samples)
            latency_count = len(self._latency_samples)
        p95 = self.p95_latency_ms()
        lines = [
            "# HELP webhook_events_total 已创建的事件数",
            "# TYPE webhook_events_total counter",
            f"webhook_events_total {self.events_total}",
            "# HELP webhook_deliveries_total 已创建的投递数（Fan-out）",
            "# TYPE webhook_deliveries_total counter",
            f"webhook_deliveries_total {self.deliveries_total}",
            "# HELP webhook_delivery_success_total 成功投递数",
            "# TYPE webhook_delivery_success_total counter",
            f"webhook_delivery_success_total {self.delivery_success}",
            "# HELP webhook_delivery_retry_total 重试投递数",
            "# TYPE webhook_delivery_retry_total counter",
            f"webhook_delivery_retry_total {self.delivery_retry}",
            "# HELP webhook_delivery_failed_total 永久失败数",
            "# TYPE webhook_delivery_failed_total counter",
            f"webhook_delivery_failed_total {self.delivery_failed}",
            "# HELP webhook_dlq_total 进入死信队列数",
            "# TYPE webhook_dlq_total counter",
            f"webhook_dlq_total {self.delivery_dlq}",
            "# HELP webhook_delivery_attempts HTTP 尝试次数",
            "# TYPE webhook_delivery_attempts counter",
            f"webhook_delivery_attempts {self.attempts_total}",
            "# HELP webhook_delivery_latency_seconds 投递处理耗时",
            "# TYPE webhook_delivery_latency_seconds summary",
            f"webhook_delivery_latency_seconds_sum {latency_sum:.6f}",
            f"webhook_delivery_latency_seconds_count {latency_count}",
            "# HELP webhook_delivery_latency_p95_ms P95 投递耗时（毫秒）",
            "# TYPE webhook_delivery_latency_p95_ms gauge",
            f"webhook_delivery_latency_p95_ms {p95 if p95 is not None else 0}",
        ]
        return "\n".join(lines) + "\n"
