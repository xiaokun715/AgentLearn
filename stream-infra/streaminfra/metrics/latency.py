"""MetricsRegistry：核心流式指标计数器与延迟统计（设计说明书 §30 / §31）。

指标：
    stream_requests_total          创建流总数
    stream_completed_total         正常完成数
    stream_failed_total            失败数
    stream_cancelled_total         取消数
    stream_disconnect_total        客户端断线数
    stream_reconnect_total         重连数
    stream_replay_total            重放数
    stream_backpressure_total      背压事件数
    stream_ttft_seconds            首 token 时延（TTFT）
    stream_total_latency_seconds   总时延（Request -> DONE）
    stream_tpot_seconds            Time Per Output Token
    stream_output_tokens           输出 token 数
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from ..core.state import StreamState

_COUNTER_NAMES = (
    "stream_requests_total",
    "stream_completed_total",
    "stream_failed_total",
    "stream_cancelled_total",
    "stream_disconnect_total",
    "stream_reconnect_total",
    "stream_replay_total",
    "stream_backpressure_total",
)


@dataclass(slots=True)
class StreamLatencyRecord:
    stream_id: str
    status: str
    ttft: float | None = None
    total_latency: float | None = None
    tpot: float | None = None
    throughput: float | None = None
    output_tokens: int = 0


class MetricsRegistry:
    def __init__(self) -> None:
        for name in _COUNTER_NAMES:
            setattr(self, name, 0)
        self._records: list[StreamLatencyRecord] = []
        self._lock = threading.Lock()  # TestClient 会跨线程读取

    # ---- 计数 ---- #
    def record_request(self, stream_id: str) -> None:
        self._inc("stream_requests_total")

    def record_disconnect(self) -> None:
        self._inc("stream_disconnect_total")

    def record_reconnect(self) -> None:
        self._inc("stream_reconnect_total")

    def record_replay(self) -> None:
        self._inc("stream_replay_total")

    def record_backpressure(self) -> None:
        self._inc("stream_backpressure_total")

    def _inc(self, name: str) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)

    # ---- 延迟 ---- #
    def record_first_token(self, stream_id: str, state: StreamState) -> None:
        """首 token 到达，记录 TTFT（Request Accepted -> First Token）。"""
        with self._lock:
            self._records.append(
                StreamLatencyRecord(stream_id=stream_id, status="running", ttft=state.ttft())
            )

    def record_finalize(
        self, stream_id: str, state: StreamState, status: str, output_tokens: int
    ) -> None:
        counter = {
            "completed": "stream_completed_total",
            "failed": "stream_failed_total",
            "cancelled": "stream_cancelled_total",
        }.get(status)
        if counter:
            self._inc(counter)

        ttft = state.ttft()
        total = state.total_latency()
        gen = (total - ttft) if (total is not None and ttft is not None) else None
        tpot = gen / output_tokens if (gen is not None and output_tokens) else None
        throughput = output_tokens / gen if (gen is not None and gen > 0) else None
        with self._lock:
            self._records.append(
                StreamLatencyRecord(
                    stream_id=stream_id,
                    status=status,
                    ttft=ttft,
                    total_latency=total,
                    tpot=tpot,
                    throughput=throughput,
                    output_tokens=output_tokens,
                )
            )

    # ---- 查询 / 导出 ---- #
    def _snapshot_records(self) -> list[StreamLatencyRecord]:
        with self._lock:
            return list(self._records)

    @staticmethod
    def _avg(records: list[StreamLatencyRecord], attr: str) -> float | None:
        vals = [getattr(r, attr) for r in records if getattr(r, attr) is not None]
        return sum(vals) / len(vals) if vals else None

    def snapshot(self) -> dict:
        records = self._snapshot_records()
        return {
            **{name: getattr(self, name) for name in _COUNTER_NAMES},
            "ttft_avg": self._avg(records, "ttft"),
            "total_latency_avg": self._avg(records, "total_latency"),
            "tpot_avg": self._avg(records, "tpot"),
            "throughput_avg": self._avg(records, "throughput"),
            "output_tokens_total": sum(r.output_tokens for r in records),
            "streams": records[-10:],
        }

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for name in _COUNTER_NAMES:
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {getattr(self, name)}")
        records = self._snapshot_records()
        summaries = {
            "stream_ttft_seconds": [r.ttft for r in records if r.ttft is not None],
            "stream_total_latency_seconds": [
                r.total_latency for r in records if r.total_latency is not None
            ],
            "stream_tpot_seconds": [r.tpot for r in records if r.tpot is not None],
            "stream_output_tokens": [r.output_tokens for r in records],
        }
        for name, vals in summaries.items():
            lines.append(f"# TYPE {name} summary")
            lines.append(f"{name}_sum {sum(vals)}")
            lines.append(f"{name}_count {len(vals)}")
        return "\n".join(lines) + "\n"
