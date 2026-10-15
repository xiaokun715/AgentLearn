"""弹性伸缩（说明书 §14）。

这个模块存在的唯一目的，是把 §14.1 那张表**跑给你看**：

    CPU          IO 密集型服务永远在 30% 附近 —— 按 CPU 扩容等于永不扩容
    QPS          忽略服务时间；上游变慢时 QPS 不变，容量却掉到 1/4
    队列深度      唯一直接反映「服务能力是否跟得上」的指标

所以本模块同时计算三种指标，但只让配置选中的那一种驱动扩容 ——
另外两种的读数会被打印出来，作为「为什么它不触发」的证据。
"""
from __future__ import annotations

from ..domain.enums import UpgradeSignal
from ..domain.models import GatewayConfig

#: IO 密集型服务的 CPU 模型：负载再高也上不去。
#: 这不是「模拟得不认真」—— 一个 1.5 秒的上游调用里，本地只花几毫秒，
#: CPU 占用几乎完全由**事件循环的空转次数**决定，与并发数几乎无关。
_CPU_BASE = 0.22
_CPU_PER_UTILISATION = 0.45
_CPU_CAP = 0.72

#: 假设的 HPA 阈值（Kubernetes 默认 80%，这里给 70% 已经很激进了）
CPU_SCALE_THRESHOLD = 0.70


class Autoscaler:
    """按选定的指标决定并发上限。"""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.base_limit = config.upstream_concurrency
        self.signal = UpgradeSignal(config.autoscale_signal)
        self._pending_limit: int | None = None
        self._pending_until_ms = 0
        self._last_scale_down_ms = 0
        self.scale_ups = 0
        self.scale_downs = 0
        #: 每次决策时记录的读数，供终端展示「为什么没触发」
        self.readings: dict[str, float] = {}
        self.scale_events: list[tuple[int, int, str]] = []

    # ------------------------------------------------------------------
    @staticmethod
    def cpu_reading(utilisation: float) -> float:
        """把并发利用率翻译成 CPU 读数（§14.1 的第一行）。"""
        return min(_CPU_CAP, _CPU_BASE + _CPU_PER_UTILISATION * min(utilisation, 1.0))

    # ------------------------------------------------------------------
    def decide(
        self,
        now_ms: int,
        *,
        queue_depth: int,
        qps: float,
        utilisation: float,
        current_limit: int,
    ) -> int | None:
        """返回新的并发上限；``None`` 表示不变。"""
        if not self.config.autoscale_enabled:
            return None

        cpu = self.cpu_reading(utilisation)
        self.readings = {
            "cpu": round(cpu, 3),
            "qps": round(qps, 2),
            "queue_depth": float(queue_depth),
            "utilisation": round(utilisation, 3),
        }

        # 冷启动的兑现（§14.2）：扩容不是免费的，新实例要 5~30 秒才可用
        if self._pending_limit is not None and now_ms >= self._pending_until_ms:
            applied = self._pending_limit
            self._pending_limit = None
            return applied

        if self._pending_limit is not None:
            return None      # 扩容在路上，别重复触发

        if self._triggered(queue_depth, qps, cpu):
            target = current_limit + self.config.autoscale_step
            self._pending_limit = target
            self._pending_until_ms = now_ms + self.config.autoscale_cold_start_ms
            self.scale_ups += 1
            self.scale_events.append((now_ms, target, self.signal.value))
            return None

        # §14.3 缩容要慢：扩容 30 秒内响应，缩容要有几分钟冷却
        if (
            queue_depth == 0
            and current_limit > self.base_limit
            and now_ms - self._last_scale_down_ms > 120_000
        ):
            self._last_scale_down_ms = now_ms
            self.scale_downs += 1
            return max(self.base_limit, current_limit - self.config.autoscale_step)

        return None

    # ------------------------------------------------------------------
    def _triggered(self, queue_depth: int, qps: float, cpu: float) -> bool:
        if self.signal is UpgradeSignal.QUEUE_DEPTH:
            return queue_depth > self.config.autoscale_queue_threshold
        if self.signal is UpgradeSignal.CPU:
            return cpu > CPU_SCALE_THRESHOLD
        # QPS：阈值取「本地上限对应的容量 × 1.2」。
        #
        # 这个取法暴露了 QPS 信号最尴尬的一面：**它测的是「已经服务了多少」，
        # 而不是「欠了多少」**。当瓶颈是我们自己设的并发上限时，实测 QPS
        # 恰好被压在阈值附近，永远触发不了扩容 —— 因为系统「看起来」正常。
        # 上游变慢后 QPS 还会下降，于是更不可能触发。
        base_capacity = self.base_limit / (self.config.service_time_estimate_ms / 1000)
        return qps > base_capacity * 1.2

    # ------------------------------------------------------------------
    @property
    def pending(self) -> tuple[int, int] | None:
        return (
            (self._pending_limit, self._pending_until_ms)
            if self._pending_limit is not None else None
        )

    def snapshot(self) -> dict:
        return {
            "signal": self.signal.value,
            "scale_ups": self.scale_ups,
            "scale_downs": self.scale_downs,
            "readings": self.readings,
            "events": self.scale_events,
            "cpu_threshold": CPU_SCALE_THRESHOLD,
        }
