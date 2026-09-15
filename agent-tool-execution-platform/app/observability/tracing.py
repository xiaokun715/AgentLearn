"""链路追踪 —— 说明书 §70 Trace。

§70 的 Trace 链路：**每一环都要开一个 span**
-------------------------------------------
::

    Agent Run            agent.run
      └─ Router          router.decide
          └─ Tool Gateway        gateway.submit
              ├─ Validation      validation.check
              ├─ Permission      permission.check
              ├─ Idempotency     idempotency.claim
              ├─ Scheduler       scheduler.enqueue / scheduler.lease
              ├─ Sandbox         sandbox.run
              ├─ Tool            tool.execute
              ├─ Result          result.process
              └─ Checkpoint      agent.checkpoint

为什么**每一环**都要是独立 span，而不是只在最外层计时
----------------------------------------------------
一次「Tool 很慢」的投诉，可能是校验卡在 LLM 自愈、可能是在排队等 Worker、
也可能是沙箱冷启动。只有把每一环切开，才能一眼看出时间花在哪一段。
把这些 span 用 ``parent_id`` 串起来之后，一次 Agent Run 就是一棵**可下钻的树**：
从根节点往下，逐层看到底是哪一环把整体拖长了 —— 这正是 §70 想要的
「从 Agent 视角一路追到 Tool 内部」。

与 ``execution_event`` 的分工
-----------------------------
:mod:`app.observability.audit` 记的是**事实**（谁在什么时候做了什么，要落库、要长期留存）；
Trace 记的是**时间与因果**（这段代码跑了多久、被谁调用，是短生命周期的运行态数据）。
两者都要有：只有审计没 Trace，排障时靠猜；只有 Trace 没审计，出了事说不清。
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from ..infra.clock import Clock, SystemClock


class SpanName:
    """§70 链路里各环节的标准 span 名 —— 拼写统一才聚合得起来。"""

    AGENT_RUN = "agent.run"
    ROUTER = "router.decide"
    TOOL_GATEWAY = "gateway.submit"
    VALIDATION = "validation.check"
    PERMISSION = "permission.check"
    IDEMPOTENCY = "idempotency.claim"
    SCHEDULER = "scheduler.enqueue"
    LEASE = "scheduler.lease"
    SANDBOX = "sandbox.run"
    TOOL = "tool.execute"
    RESULT = "result.process"
    CHECKPOINT = "agent.checkpoint"


@dataclass
class Span:
    """一段被追踪的工作（§70）。

    :param name: span 名（见 :class:`SpanName`）
    :param trace_id: 一次 Agent Run 的全局 ID，同一棵树里的 span 共享它
    :param span_id: 本 span 的短 ID
    :param parent_id: 父 span 的 ID；``None`` 表示这是根 span
    :param started_at: 起始 epoch 秒
    :param ended_at: 结束 epoch 秒；``None`` 表示还没结束（仍在执行）
    :param attributes: 结构化标签（tool_name / call_id / attempt …）
    :param events: span 内的时间点事件（如「重试第 2 次」「沙箱就绪」）
    :param status: ``OK`` / ``ERROR``
    """

    name: str
    trace_id: str
    span_id: str
    parent_id: Optional[str]
    started_at: float
    ended_at: Optional[float] = None
    attributes: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    status: str = "OK"

    def duration_ms(self) -> float:
        """耗时（毫秒）。未结束的 span 返回 ``0.0`` —— 不要用「当前时间减起始」
        去糊弄：那会让正在执行的长任务看起来比已经结束的还久。"""
        if self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at) * 1000.0

    def to_dict(self) -> dict:
        """导出成 JSON-safe 的 dict（后续可接 Jaeger / OTLP）。"""
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms(),
            "status": self.status,
            "attributes": dict(self.attributes),
            "events": [dict(event) for event in self.events],
        }


class Tracer:
    """极简 tracer（§70）。

    :param enabled: ``False`` 时不再记录 span（零开销路径）。
        但 :meth:`span` 仍会返回一个可用的 :class:`Span` 对象 ——
        调用点不需要为了「追踪开关」写两套代码。
    :param clock: 可注入时钟，让耗时在推演里可复现
    """

    def __init__(self, *, enabled: bool = True, clock: Optional[Clock] = None) -> None:
        self.enabled = enabled
        self.clock = clock or SystemClock()
        self._spans: dict[str, list[Span]] = {}

    # ==================================================================
    # 生命周期
    # ==================================================================
    def new_trace(self) -> str:
        """开一条新链路，返回 ``trace_id``（16 位十六进制）。"""
        return uuid.uuid4().hex[:16]

    def start_span(
        self,
        name: str,
        *,
        trace_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        **attributes: Any,
    ) -> Span:
        """开一个 span；``trace_id`` 缺省表示「这是一条新链路的根」。"""
        span = Span(
            name=name,
            trace_id=trace_id or self.new_trace(),
            span_id=uuid.uuid4().hex[:8],
            parent_id=parent_id,
            started_at=self.clock.time(),
            attributes=dict(attributes),
        )
        if self.enabled:
            self._spans.setdefault(span.trace_id, []).append(span)
        return span

    def end_span(self, span: Span, *, status: str = "OK", **attributes: Any) -> Span:
        """结束一个 span，合并收尾属性（如 ``result_status`` / ``attempt``）。"""
        span.ended_at = self.clock.time()
        span.status = status
        span.attributes.update(attributes)
        return span

    def add_event(self, span: Span, name: str, **payload: Any) -> None:
        """在 span 上打一个时间点事件 —— 用来记录「过程中发生了什么」。

        典型用途：``tracer.add_event(span, "retry.scheduled", attempt=2, backoff_ms=2000)``。
        它比 attribute 多带一个时间戳，因而能看出「重试之间隔了多久」。
        """
        span.events.append({"name": name, "at": self.clock.time(), **payload})

    @contextmanager
    def span(
        self,
        name: str,
        *,
        trace_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        **attributes: Any,
    ) -> Iterator[Span]:
        """上下文管理器形态的 span —— 退出时自动收尾。

        ::

            with tracer.span(SpanName.TOOL, tool_name="run_test") as sp:
                result = run()
                tracer.add_event(sp, "tool.done")

        **异常必须重新抛出**：追踪的职责是「记录发生了什么」，不是「决定要不要吞掉异常」。
        吞掉异常会让上层以为执行成功了 —— 这比没有追踪危险得多。
        这里把异常信息记进 span（``status="ERROR"`` + ``exception`` 事件）之后就原样抛出。
        """
        span = self.start_span(
            name, trace_id=trace_id, parent_id=parent_id, **attributes
        )
        try:
            yield span
        except BaseException as exc:  # noqa: BLE001 - 记录后必须原样抛出
            self.add_event(
                span,
                "exception",
                type=type(exc).__name__,
                message=str(exc),
            )
            self.end_span(span, status="ERROR", error_type=type(exc).__name__)
            raise
        else:
            self.end_span(span)
        finally:
            # 兜底：即使上面的分支被跳过了，也保证 ended_at 不会永久缺失
            if span.ended_at is None:
                self.end_span(span)

    # ==================================================================
    # 读取 / 导出
    # ==================================================================
    def spans(self, trace_id: str) -> list[Span]:
        """一条链路里的全部 span（按开始顺序，即天然的父子先后顺序）。"""
        return list(self._spans.get(trace_id, []))

    def export(self) -> list[dict]:
        """导出全部 span（JSON-safe）—— 直接喂给可视化或落盘。"""
        return [
            span.to_dict()
            for trace_id in sorted(self._spans)
            for span in self._spans[trace_id]
        ]

    def trace_ids(self) -> list[str]:
        """当前持有的全部 trace_id —— 演示收尾时用来遍历。"""
        return sorted(self._spans)

    def reset(self) -> None:
        """清空 —— 演示分段 / 测试隔离。"""
        self._spans.clear()
