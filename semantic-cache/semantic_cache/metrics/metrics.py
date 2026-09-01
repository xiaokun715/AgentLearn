"""Cache Metrics（设计说明书 §41 ~ §46）。

Semantic Cache 必须做指标，且不能只看 Hit Rate：
  Hit Rate = 90% 但如果 False Hit = 10%，这套缓存反而危险（§43）。
因此至少同时观察：Hit Rate / False Hit Rate / Token Saved / Cost Saved / Latency Saved。

本实现是一个进程内计数器 + Prometheus 文本渲染（无外部依赖）。
生产可替换为 prometheus_client 后接 Grafana。
"""
from __future__ import annotations

import threading
from collections import Counter

_LATENCY_WINDOW = 10_000  # 相似度 / 延迟样本的滑动窗口上限，防止内存无限增长


class CacheMetrics:
    def __init__(
        self,
        *,
        input_cost_per_million: float = 1.0,
        output_cost_per_million: float = 5.0,
    ):
        self._lock = threading.Lock()

        # §41 核心计数器
        self.requests_total: int = 0
        self.hits_total: int = 0
        self.exact_hits_total: int = 0
        self.semantic_hits_total: int = 0
        self.misses_total: int = 0
        self.sets_total: int = 0
        self.skipped_total: int = 0
        self.skipped_reasons: Counter[str] = Counter()
        self.false_hits_total: int = 0
        self.eviction_total: int = 0
        self.expiration_total: int = 0

        # §44 ~ §46 价值换算
        self.tokens_saved: int = 0
        self.cost_saved_usd: float = 0.0
        self.latency_samples_ms: list[float] = []
        self.similarity_samples: list[float] = []

        self._input_cost_per_m = input_cost_per_million
        self._output_cost_per_m = output_cost_per_million

    # ---- 记录 ----------------------------------------------------------

    def record_request(self) -> None:
        with self._lock:
            self.requests_total += 1

    def record_hit(self, source: str, *, response: dict | None = None) -> None:
        with self._lock:
            self.hits_total += 1
            if source == "exact":
                self.exact_hits_total += 1
            else:
                self.semantic_hits_total += 1
        if response:
            self.record_saved(response)

    def record_miss(self) -> None:
        with self._lock:
            self.misses_total += 1

    def record_set(self) -> None:
        with self._lock:
            self.sets_total += 1

    def record_skipped(self, reason: str) -> None:
        with self._lock:
            self.skipped_total += 1
            self.skipped_reasons[reason] += 1

    def record_false_hit(self) -> None:
        """潜在错误命中（§43）。可在线下评估/用户反馈时手动调用。"""
        with self._lock:
            self.false_hits_total += 1

    def record_eviction(self, n: int = 1) -> None:
        with self._lock:
            self.eviction_total += n

    def record_expiration(self, n: int = 1) -> None:
        with self._lock:
            self.expiration_total += n

    def record_similarity(self, value: float) -> None:
        with self._lock:
            self.similarity_samples.append(value)
            if len(self.similarity_samples) > _LATENCY_WINDOW:
                self.similarity_samples.pop(0)

    def record_latency(self, milliseconds: float) -> None:
        with self._lock:
            self.latency_samples_ms.append(milliseconds)
            if len(self.latency_samples_ms) > _LATENCY_WINDOW:
                self.latency_samples_ms.pop(0)

    def record_saved(self, response: dict) -> None:
        """记录一次缓存命中避免的 token / 成本（§44 ~ §45）。

        response 需携带 OpenAI 风格的 usage：{"prompt_tokens":N,"completion_tokens":M}。
        """
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            return
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        with self._lock:
            self.tokens_saved += prompt + completion
            self.cost_saved_usd += (
                prompt / 1_000_000 * self._input_cost_per_m
                + completion / 1_000_000 * self._output_cost_per_m
            )

    # ---- 派生指标 ------------------------------------------------------

    def hit_rate(self) -> float:
        if self.requests_total == 0:
            return 0.0
        return self.hits_total / self.requests_total

    def avg_latency_ms(self) -> float:
        if not self.latency_samples_ms:
            return 0.0
        return sum(self.latency_samples_ms) / len(self.latency_samples_ms)

    def avg_similarity(self) -> float:
        if not self.similarity_samples:
            return 0.0
        return sum(self.similarity_samples) / len(self.similarity_samples)

    # ---- 输出 ----------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        return {
            "requests_total": self.requests_total,
            "hits_total": self.hits_total,
            "exact_hits_total": self.exact_hits_total,
            "semantic_hits_total": self.semantic_hits_total,
            "misses_total": self.misses_total,
            "hit_rate": round(self.hit_rate(), 4),
            "sets_total": self.sets_total,
            "skipped_total": self.skipped_total,
            "skipped_reasons": dict(self.skipped_reasons),
            "false_hits_total": self.false_hits_total,
            "eviction_total": self.eviction_total,
            "expiration_total": self.expiration_total,
            "tokens_saved": self.tokens_saved,
            "cost_saved_usd": round(self.cost_saved_usd, 6),
            "avg_latency_ms": round(self.avg_latency_ms(), 2),
            "avg_similarity": round(self.avg_similarity(), 4),
        }

    def render_prometheus(self) -> str:
        s = self.snapshot()
        lines: list[str] = []

        def emit(name: str, doc: str, type_: str, value: object) -> None:
            lines.append(f"# HELP semantic_cache_{name} {doc}")
            lines.append(f"# TYPE semantic_cache_{name} {type_}")
            lines.append(f"semantic_cache_{name} {value}")

        emit("requests_total", "Semantic Cache 请求总数", "counter", s["requests_total"])
        emit("hits_total", "命中总数", "counter", s["hits_total"])
        emit("exact_hits_total", "精确命中数", "counter", s["exact_hits_total"])
        emit("semantic_hits_total", "语义命中数", "counter", s["semantic_hits_total"])
        emit("misses_total", "未命中数", "counter", s["misses_total"])
        emit("hit_rate", "缓存命中率", "gauge", s["hit_rate"])
        emit("sets_total", "写入缓存条数", "counter", s["sets_total"])
        emit("skipped_total", "不可缓存被跳过数", "counter", s["skipped_total"])
        emit("false_hits_total", "潜在错误命中数", "counter", s["false_hits_total"])
        emit("eviction_total", "主动失效删除条数", "counter", s["eviction_total"])
        emit("expiration_total", "TTL 过期条数", "counter", s["expiration_total"])
        emit("tokens_saved", "累计节省 token 数", "counter", s["tokens_saved"])
        emit("cost_saved_usd", "累计节省成本（美元）", "counter", s["cost_saved_usd"])
        emit("avg_latency_ms", "平均命中延迟（毫秒）", "gauge", s["avg_latency_ms"])
        emit("avg_similarity", "平均命中相似度", "gauge", s["avg_similarity"])
        return "\n".join(lines) + "\n"
