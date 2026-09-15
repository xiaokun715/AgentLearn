"""指标采集 —— 说明书 §70 Metrics（配合 §69 的 Prometheus 暴露）。

§70 列出的 14 个指标就是这套系统的「体检表」
-------------------------------------------
::

    tool_call_total              调用总量（按 tool / status 拆）
    tool_success_total           成功数
    tool_failure_total           失败数（按 error_type 拆 —— 分类统计才看得出是哪类病）
    tool_retry_total             重试次数（§36）
    tool_timeout_total           超时数（§35）
    tool_latency_ms              端到端耗时（含排队）
    tool_queue_wait_ms           排队等待（§4.2 异步队列）
    tool_execution_ms            真正执行耗时（沙箱内）
    tool_duplicate_total         重复执行命中（§30）
    tool_cycle_total             循环检测命中（§31）
    tool_human_intervention_total 人工介入次数（§51）
    tool_idempotency_hit_total   幂等命中（§12）
    tool_lease_expired_total     租约过期（§56）
    tool_recovery_total          恢复动作次数（§35）

**latency 与 execution 必须分开**：一个耗时 60s 的请求里可能有 55s 在排队。
只记总量的话，「加了 Worker 反而更慢」这种问题永远查不出来 ——
是执行变慢了，还是队列变长了？拆开就一目了然。

设计取舍
--------
* **零依赖**：不引入 prometheus_client。§69 要的只是「能被 Prometheus 抓取」，
  文本格式本身很简单，自己渲染反而没有版本兼容负担。
* **标签是有序元组**：``(name, tuple(sorted(labels.items())))`` 做键 ——
  ``f(tool="a", status="ok")`` 与 ``f(status="ok", tool="a")`` 必须落到同一个格子，
  否则每一次调用顺序不同就会凭空长出新的时间序列。
* **``enabled=False`` 时全部写入是 no-op**：这条路径要足够短，
  才能在「指标系统自己出问题」的时候被安全地关掉而不拖慢主链路。
"""
from __future__ import annotations

from typing import Any, Optional


class MetricName:
    """§70 定义的指标名（**一个都不能少**）。"""

    # ---- 计数类 ----
    TOOL_CALL_TOTAL = "tool_call_total"
    TOOL_SUCCESS_TOTAL = "tool_success_total"
    TOOL_FAILURE_TOTAL = "tool_failure_total"
    TOOL_RETRY_TOTAL = "tool_retry_total"
    TOOL_TIMEOUT_TOTAL = "tool_timeout_total"
    TOOL_DUPLICATE_TOTAL = "tool_duplicate_total"
    TOOL_CYCLE_TOTAL = "tool_cycle_total"
    TOOL_HUMAN_INTERVENTION_TOTAL = "tool_human_intervention_total"
    TOOL_IDEMPOTENCY_HIT_TOTAL = "tool_idempotency_hit_total"
    TOOL_LEASE_EXPIRED_TOTAL = "tool_lease_expired_total"
    TOOL_RECOVERY_TOTAL = "tool_recovery_total"

    # ---- 直方图类（毫秒）----
    TOOL_LATENCY_MS = "tool_latency_ms"
    TOOL_QUEUE_WAIT_MS = "tool_queue_wait_ms"
    TOOL_EXECUTION_MS = "tool_execution_ms"


# 模块级别名，方便 `from app.observability.metrics import TOOL_CALL_TOTAL`
TOOL_CALL_TOTAL = MetricName.TOOL_CALL_TOTAL
TOOL_SUCCESS_TOTAL = MetricName.TOOL_SUCCESS_TOTAL
TOOL_FAILURE_TOTAL = MetricName.TOOL_FAILURE_TOTAL
TOOL_RETRY_TOTAL = MetricName.TOOL_RETRY_TOTAL
TOOL_TIMEOUT_TOTAL = MetricName.TOOL_TIMEOUT_TOTAL
TOOL_DUPLICATE_TOTAL = MetricName.TOOL_DUPLICATE_TOTAL
TOOL_CYCLE_TOTAL = MetricName.TOOL_CYCLE_TOTAL
TOOL_HUMAN_INTERVENTION_TOTAL = MetricName.TOOL_HUMAN_INTERVENTION_TOTAL
TOOL_IDEMPOTENCY_HIT_TOTAL = MetricName.TOOL_IDEMPOTENCY_HIT_TOTAL
TOOL_LEASE_EXPIRED_TOTAL = MetricName.TOOL_LEASE_EXPIRED_TOTAL
TOOL_RECOVERY_TOTAL = MetricName.TOOL_RECOVERY_TOTAL
TOOL_LATENCY_MS = MetricName.TOOL_LATENCY_MS
TOOL_QUEUE_WAIT_MS = MetricName.TOOL_QUEUE_WAIT_MS
TOOL_EXECUTION_MS = MetricName.TOOL_EXECUTION_MS

#: §70 全部指标名（计数 + 直方图），顺序即文档顺序。
ALL_METRIC_NAMES: tuple[str, ...] = (
    TOOL_CALL_TOTAL,
    TOOL_SUCCESS_TOTAL,
    TOOL_FAILURE_TOTAL,
    TOOL_RETRY_TOTAL,
    TOOL_TIMEOUT_TOTAL,
    TOOL_LATENCY_MS,
    TOOL_QUEUE_WAIT_MS,
    TOOL_EXECUTION_MS,
    TOOL_DUPLICATE_TOTAL,
    TOOL_CYCLE_TOTAL,
    TOOL_HUMAN_INTERVENTION_TOTAL,
    TOOL_IDEMPOTENCY_HIT_TOTAL,
    TOOL_LEASE_EXPIRED_TOTAL,
    TOOL_RECOVERY_TOTAL,
)

#: 直方图语义（count / sum / min / max）的指标；其余都是单调整数计数器。
HISTOGRAM_METRIC_NAMES: frozenset[str] = frozenset(
    {TOOL_LATENCY_MS, TOOL_QUEUE_WAIT_MS, TOOL_EXECUTION_MS}
)


class _Histogram:
    """一个时间序列上的观测聚合（Prometheus 的 Summary 语义）。"""

    __slots__ = ("count", "sum", "min", "max")

    def __init__(self) -> None:
        self.count = 0
        self.sum = 0.0
        self.min: Optional[float] = None
        self.max: Optional[float] = None

    def observe(self, value: float) -> None:
        self.count += 1
        self.sum += value
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "sum": self.sum,
            "min": self.min,
            "max": self.max,
            "avg": (self.sum / self.count) if self.count else 0.0,
        }


class Metrics:
    """进程内指标登记处（§70）。

    :param enabled: ``False`` 时所有写入退化为 no-op（零开销路径）。
        读操作（``get`` / ``snapshot`` / ``render_prometheus``）仍可用，
        只是拿到空数据 —— 关闭采集不该让观测接口本身按下停止键。
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], _Histogram] = {}

    # ==================================================================
    # 基础写入
    # ==================================================================
    def incr(self, name: str, value: int = 1, **labels: str) -> None:
        """计数器 +1（或 +``value``）。"""
        if not self.enabled:
            return
        key = self._key(name, labels)
        self._counters[key] = self._counters.get(key, 0.0) + float(value)

    def observe(self, name: str, value: float, **labels: str) -> None:
        """记录一次观测值 —— **直方图语义**，维护 ``count`` / ``sum`` / ``min`` / ``max``。

        只存聚合值而不是保留原始样本：指标接口会被高频调用，
        留着每一次样本等于给自己装了个内存泄漏；而 ``min`` / ``max``
        已经足够回答排障时最常问的两个问题（「最坏有多坏」「有没有卡死」）。
        """
        if not self.enabled:
            return
        key = self._key(name, labels)
        bucket = self._histograms.get(key)
        if bucket is None:
            bucket = _Histogram()
            self._histograms[key] = bucket
        bucket.observe(float(value))

    # ==================================================================
    # 读取
    # ==================================================================
    def get(self, name: str, **labels: str) -> float:
        """读一个序列的值。

        计数器返回累计值；直方图返回**观测次数**（想要完整分布请用 :meth:`snapshot`）。
        这样 ``get`` 对两种指标都有「越大越有事」的可读语义。
        """
        key = self._key(name, labels)
        if key in self._counters:
            return self._counters[key]
        bucket = self._histograms.get(key)
        return float(bucket.count) if bucket is not None else 0.0

    def snapshot(self) -> dict[str, Any]:
        """全量快照 —— 给观测 API / 演示收尾打印用。

        ``counters`` 与 ``histograms`` 都渲染成**列表**而不是按名字嵌套的 dict：
        过滤、排序、塞进 JSON 前端都更省事，也不会因为标签编码方式（``a=1,b=2``）
        而被误当成稳定的键。
        """
        counters = [
            {"name": name, "labels": dict(labels), "value": value}
            for (name, labels), value in sorted(
                self._counters.items(), key=lambda kv: (kv[0][0], kv[0][1])
            )
        ]
        histograms = [
            {"name": name, "labels": dict(labels), **bucket.to_dict()}
            for (name, labels), bucket in sorted(
                self._histograms.items(), key=lambda kv: (kv[0][0], kv[0][1])
            )
        ]
        return {
            "enabled": self.enabled,
            "counters": counters,
            "histograms": histograms,
            "total_series": len(counters) + len(histograms),
        }

    def render_prometheus(self) -> str:
        """渲染成 Prometheus 文本暴露格式（§69 的 ``/metrics`` 就返回它）。

        ::

            # TYPE tool_call_total counter
            tool_call_total{tool="run_test",status="succeeded"} 3
            # TYPE tool_latency_ms histogram
            tool_latency_ms_count{tool="run_test"} 2
            tool_latency_ms_sum{tool="run_test"} 120.5

        直方图额外输出 ``_min`` / ``_max``：它们不是 Prometheus 的标准后缀，
        但排障时「最坏一次卡了多久」比平均值有用得多，多两行很划算。
        """
        lines: list[str] = []
        for name in ALL_METRIC_NAMES:
            is_hist = name in HISTOGRAM_METRIC_NAMES
            lines.append(f"# TYPE {name} {'histogram' if is_hist else 'counter'}")

            if is_hist:
                rows = [
                    (labels, bucket)
                    for (series, labels), bucket in sorted(
                        self._histograms.items(), key=lambda kv: kv[0][1]
                    )
                    if series == name
                ]
                for labels, bucket in rows:
                    rendered = _render_labels(labels)
                    lines.append(f"{name}_count{rendered} {bucket.count}")
                    lines.append(f"{name}_sum{rendered} {_num(bucket.sum)}")
                    lines.append(f"{name}_min{rendered} {_num(bucket.min or 0.0)}")
                    lines.append(f"{name}_max{rendered} {_num(bucket.max or 0.0)}")
            else:
                rows = [
                    (labels, value)
                    for (series, labels), value in sorted(
                        self._counters.items(), key=lambda kv: kv[0][1]
                    )
                    if series == name
                ]
                for labels, value in rows:
                    lines.append(f"{name}{_render_labels(labels)} {_num(value)}")

        return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        """清空全部序列 —— 演示分段、测试之间互不串味。"""
        self._counters.clear()
        self._histograms.clear()

    # ==================================================================
    # §70 指标的便捷方法（调用点不必记字符串常量）
    # ==================================================================
    def tool_call(self, tool_name: str, status: str = "", **labels: str) -> None:
        """一次工具调用（无论成败）—— 分母指标。"""
        self.incr(TOOL_CALL_TOTAL, tool_name=tool_name, status=status, **labels)

    def tool_success(self, tool_name: str, **labels: str) -> None:
        """一次成功执行。"""
        self.incr(TOOL_SUCCESS_TOTAL, tool_name=tool_name, **labels)

    def tool_failure(self, tool_name: str, error_type: str = "", **labels: str) -> None:
        """一次失败执行；``error_type`` 带上 §34 分类，失败才有可分析性。"""
        self.incr(TOOL_FAILURE_TOTAL, tool_name=tool_name, error_type=error_type, **labels)

    def tool_retry(self, tool_name: str, attempt: int = 0, error_type: str = "", **labels: str) -> None:
        """一次重试（§36）—— 重试率突增通常先于故障暴露。"""
        self.incr(
            TOOL_RETRY_TOTAL,
            tool_name=tool_name,
            attempt=str(attempt),
            error_type=error_type,
            **labels,
        )

    def tool_timeout(self, tool_name: str, **labels: str) -> None:
        """一次超时（§35）。"""
        self.incr(TOOL_TIMEOUT_TOTAL, tool_name=tool_name, **labels)

    def tool_latency(self, tool_name: str, ms: float, **labels: str) -> None:
        """端到端耗时（含排队）。"""
        self.observe(TOOL_LATENCY_MS, ms, tool_name=tool_name, **labels)

    def tool_queue_wait(self, tool_name: str, ms: float, **labels: str) -> None:
        """排队等待耗时 —— 与执行耗时分开看，才能判断瓶颈在队列还是在执行。"""
        self.observe(TOOL_QUEUE_WAIT_MS, ms, tool_name=tool_name, **labels)

    def tool_execution(self, tool_name: str, ms: float, **labels: str) -> None:
        """沙箱内真正执行的耗时。"""
        self.observe(TOOL_EXECUTION_MS, ms, tool_name=tool_name, **labels)

    def tool_duplicate(self, tool_name: str, signal: str = "", **labels: str) -> None:
        """重复执行命中（§30），``signal`` 记录升级到哪一级。"""
        self.incr(TOOL_DUPLICATE_TOTAL, tool_name=tool_name, signal=signal, **labels)

    def tool_cycle(self, signal: str = "", run_id: str = "", **labels: str) -> None:
        """DAG 循环命中（§31）—— 按 run 计数，环是「某次运行」的属性。"""
        self.incr(TOOL_CYCLE_TOTAL, run_id=run_id, signal=signal, **labels)

    def tool_human_intervention(self, tool_name: str, decision: str = "", **labels: str) -> None:
        """人工介入（§51-§53）；``decision`` 取 approve / reject / modify。"""
        self.incr(
            TOOL_HUMAN_INTERVENTION_TOTAL,
            tool_name=tool_name,
            decision=decision,
            **labels,
        )

    def tool_idempotency_hit(self, tool_name: str, **labels: str) -> None:
        """幂等命中（§12）—— Agent 重复提交被干净地吃掉，是**好事**，不是错误。"""
        self.incr(TOOL_IDEMPOTENCY_HIT_TOTAL, tool_name=tool_name, **labels)

    def tool_lease_expired(self, tool_name: str, **labels: str) -> None:
        """租约过期（§56）—— 每一次都意味着可能有 Worker 没死透。"""
        self.incr(TOOL_LEASE_EXPIRED_TOTAL, tool_name=tool_name, **labels)

    def tool_recovery(self, tool_name: str, action: str = "", error_type: str = "", **labels: str) -> None:
        """一次恢复决策落地（§35）；``action`` 是 RETRY / FALLBACK / HUMAN / ABORT。"""
        self.incr(
            TOOL_RECOVERY_TOTAL,
            tool_name=tool_name,
            action=action,
            error_type=error_type,
            **labels,
        )

    # ==================================================================
    # 内部
    # ==================================================================
    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
        """标签排序后做键（理由见模块 docstring）。"""
        return name, tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    """``{a="1",b="2"}``；无标签时是空串（Prometheus 允许省略花括号）。"""
    if not labels:
        return ""
    inner = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    """按 Prometheus 文本格式转义标签值。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _num(value: float) -> str:
    """整数不显示小数点（``3`` 比 ``3.0`` 更接近 Prometheus 的习惯输出）。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")
