"""Tracing —— 极简 trace 上下文（设计说明书 §36 可观测性延伸）。

真正的生产环境接入 OpenTelemetry / LangSmith；这里用 contextvars
给每次 Job 执行打 trace_id + span 树，并落到结构化日志里。
"""
from __future__ import annotations

import contextvars
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterator

from contextlib import contextmanager

_logger = logging.getLogger(__name__)

_current_trace: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar(
    "current_trace", default=None
)


@dataclass
class Span:
    name: str
    start: float = 0.0
    parent: "Span | None" = None
    children: list["Span"] = field(default_factory=list)


@dataclass
class Trace:
    trace_id: str
    job_id: str
    root: Span | None = None
    stack: list[Span] = field(default_factory=list)


class Tracer:
    """为每个 Job 生成一个 Trace，span 支持嵌套。"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("app.trace")

    def start_trace(self, job_id: str) -> Trace:
        trace = Trace(trace_id=f"tr_{uuid.uuid4().hex[:10]}", job_id=job_id)
        self._logger.info("trace_start trace=%s job=%s", trace.trace_id, job_id)
        return trace

    @contextmanager
    def span(self, trace: Trace, name: str) -> Iterator[Span]:
        span = Span(name=name, start=time.monotonic())
        parent = trace.stack[-1] if trace.stack else None
        if parent is not None:
            parent.children.append(span)
        elif trace.root is None:
            trace.root = span
        trace.stack.append(span)

        _token = _current_trace.set(trace)
        try:
            self._logger.debug("span_start trace=%s name=%s", trace.trace_id, name)
            yield span
        finally:
            trace.stack.pop()
            _current_trace.reset(_token)
            self._logger.debug(
                "span_end trace=%s name=%s duration_ms=%.1f",
                trace.trace_id, name, (time.monotonic() - span.start) * 1000,
            )

    @classmethod
    def current_trace_id(cls) -> str | None:
        trace = _current_trace.get()
        return trace.trace_id if trace else None
