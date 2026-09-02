"""Metrics（设计说明书 §38）。

计数器 + P50/P95/P99 延迟，Prometheus 文本格式导出（GET /metrics）。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


def _labels(labels: dict | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((labels or {}).items()))


class Metrics:
    def __init__(self, latency_window: int = 1000) -> None:
        self._counters: dict[tuple[str, tuple], int] = defaultdict(int)
        self._samples: dict[tuple[str, tuple], deque] = defaultdict(
            lambda: deque(maxlen=latency_window)
        )
        self._lock = threading.Lock()

    # ---- 计数 ---------------------------------------------------------------
    def inc(self, name: str, labels: dict | None = None, value: int = 1) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._counters[key] += value

    # ---- 延迟 ---------------------------------------------------------------
    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._samples[key].append(value)

    def time(self, name: str, labels: dict | None = None):
        """上下文管理器计时：with metrics.time("guardrail_latency"): ..."""
        t0 = time.perf_counter()

        class _Ctx:
            def __enter__(self_):  # noqa: N805
                return self_

            def __exit__(self_, *exc):  # noqa: N805
                self.observe(name, time.perf_counter() - t0, labels)
                return False

        return _Ctx()

    # ---- 快照 ---------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            counters = {
                f"{name}{_fmt_labels(labels)}": value
                for (name, labels), value in sorted(self._counters.items())
            }
        return counters

    # ---- 渲染 ---------------------------------------------------------------
    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = sorted(
                (name, labels, value)
                for (name, labels), value in self._counters.items()
            )
        for name, labels, value in counters:
            lines.append(f"{name}{_fmt_labels(labels)} {value}")

        # 延迟汇总（P50/P95/P99）
        for key, samples in sorted(self._samples.items()):
            if not samples:
                continue
            name, labels = key
            ordered = sorted(samples)
            for q, label in ((0.50, "p50"), (0.95, "p95"), (0.99, "p99")):
                lines.append(
                    f"{name}_{label}{_fmt_labels(labels)} {self._quantile(ordered, q):.6f}"
                )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _quantile(sorted_samples: list, q: float) -> float:
        if len(sorted_samples) == 1:
            return float(sorted_samples[0])
        idx = (len(sorted_samples) - 1) * q
        lo = int(idx)
        hi = min(lo + 1, len(sorted_samples) - 1)
        frac = idx - lo
        return sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac


def _fmt_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in labels)
    return "{" + inner + "}"


__all__ = ["Metrics"]
