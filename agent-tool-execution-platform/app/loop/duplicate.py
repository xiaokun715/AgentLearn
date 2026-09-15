"""重复 Tool 执行检测 —— 说明书 §30 Duplicate Tool Execution Detection。

§30 的判据是四个 same
---------------------
::

    同一个 Tool  +  同一组参数  +  同一个图步  +  短时间窗内连续出现

四个条件缺一不可，少一个都会把**正常行为**误判成循环：

* 少了「同一组参数」：轮询 ``get_status`` 会被判成循环 —— 它本来就该重复调。
* 少了「短时间窗」：今天早上调了 3 次 ``run_test`` 是三条独立的需求，
  跟「1 秒内调了 3 次」完全是两码事。**超出窗口的计数必须清零重来**，
  否则计数会随时间单调累积，系统跑得越久越容易「假死」。
* 少了「同一图步」：同一个 run 里 A 步和 B 步各调一次同样的查询是合法意图
  （§7 的 ``logical_step_id`` 讲的是同一件事），不该被合并计数。

本模块只做**检测与建议**，不做拦截
----------------------------------
它返回 :class:`DuplicateVerdict`（含 §32 的分级信号与 §33 的可执行降级建议），
由调用方（Tool Node / Scheduler）决定是继续、降级还是停。
理由：检测器看不到全局（不知道 Agent 还能不能换别的路子），
**把「判断」和「决定」分开，才能在不停机的前提下逐级升级**。

``history()`` 就是 §30 说的 ``Execution History``：完整的「谁、哪一步、什么参数、
什么结果」流水，既是审计材料，也是排障时判断「是不是真卡住了」的唯一依据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import AppConfig
from ..domain.enums import LoopSignal
from ..domain.models import arguments_hash
from ..infra.clock import Clock, SystemClock


@dataclass
class DuplicateVerdict:
    """一次重复检测的结论。

    :param signal: §32 分级信号（OK / WARNING / DEGRADED / STOP）
    :param count: 当前时间窗内，同一 ``(run, step, signature)`` 出现了几次
    :param signature: 执行签名 ``f"{tool_name}:{arguments_hash(arguments)}"``
    :param window_seconds: 本次判定使用的短时间窗长度
    :param reason: 可直接读的中文结论
    :param degradation: §33 的**可执行**降级建议（空 dict 表示无需降级）
    """

    signal: LoopSignal
    count: int
    signature: str
    window_seconds: float
    reason: str
    degradation: dict = field(default_factory=dict)

    @property
    def should_stop(self) -> bool:
        return self.signal is LoopSignal.STOP

    def to_dict(self) -> dict:
        return {
            "signal": self.signal.value,
            "count": self.count,
            "signature": self.signature,
            "window_seconds": self.window_seconds,
            "reason": self.reason,
            "degradation": self.degradation,
        }


@dataclass
class _Entry:
    """一条执行流水（§30 的 Execution History 元素）。"""

    run_id: str
    step_id: str
    tool_name: str
    signature: str
    status: str
    timestamp: float
    result_hash: Optional[str] = None
    arguments: dict = field(default_factory=dict)


class DuplicateDetector:
    """§30 重复执行检测器。

    :param config: 平台配置，阈值取自 ``config.loop``（``configs/recovery.yaml``）
    :param clock: 可注入时钟 —— 「短时间窗」是行为的核心，
        用 :class:`~app.infra.clock.ManualClock` 拨快时间就能把
        「窗口过期 -> 计数清零」演示清楚，不必真的 sleep
    """

    def __init__(self, config: AppConfig, *, clock: Optional[Clock] = None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self._history: dict[str, list[_Entry]] = {}

    # ==================================================================
    # 检测
    # ==================================================================
    def record(
        self,
        *,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: dict,
        status: str = "",
        result_hash: Optional[str] = None,
    ) -> DuplicateVerdict:
        """记一次执行并给出本次的重复判定（§30）。

        计数键是 ``(run_id, step_id, signature)`` —— 前两者把「哪个 run 的哪一步」
        定住，签名把「同 Tool + 同参数」定住，剩下的「短时间窗」由时钟兜住。
        """
        policy = self.config.loop
        now = self.clock.time()
        signature = f"{tool_name}:{arguments_hash(arguments)}"

        entry = _Entry(
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            signature=signature,
            status=status,
            timestamp=now,
            result_hash=result_hash,
            arguments=dict(arguments or {}),
        )
        self._history.setdefault(run_id, []).append(entry)

        # 只数落在窗口内的记录：窗口外的历史仍然留在流水里（审计要完整），
        # 但**不参与计数** —— 否则「今天调过 3 次」会被误判成循环。
        window = policy.window_seconds
        count = sum(
            1
            for item in self._history[run_id]
            if item.step_id == step_id
            and item.signature == signature
            and now - item.timestamp <= window
        )

        signal = self._signal_for(count)
        degradation = self._degradation(tool_name, arguments, signal)
        return DuplicateVerdict(
            signal=signal,
            count=count,
            signature=signature,
            window_seconds=window,
            reason=self._reason(
                signal=signal,
                count=count,
                tool_name=tool_name,
                step_id=step_id,
                window=window,
                degradation=degradation,
            ),
            degradation=degradation,
        )

    def history(self, run_id: str) -> list[dict]:
        """该 run 的完整执行流水（§30 ``Execution History``），按发生顺序。"""
        return [
            {
                "run_id": entry.run_id,
                "step_id": entry.step_id,
                "tool_name": entry.tool_name,
                "signature": entry.signature,
                "status": entry.status,
                "timestamp": entry.timestamp,
                "result_hash": entry.result_hash,
                "arguments": entry.arguments,
            }
            for entry in self._history.get(run_id, [])
        ]

    def count(self, run_id: str, signature: str) -> int:
        """该 run 内某签名的**窗口内**出现次数（跨步骤聚合）。

        与 :meth:`record` 返回的 ``count`` 的区别：这里不带 ``step_id``，
        用于回答「这个 run 是不是整体在打转」这种更粗的问题。
        """
        now = self.clock.time()
        window = self.config.loop.window_seconds
        return sum(
            1
            for item in self._history.get(run_id, [])
            if item.signature == signature and now - item.timestamp <= window
        )

    def reset(self, run_id: str) -> None:
        """清空某个 run 的流水 —— 新一轮任务开始（或人工介入后重新放行）时调用。"""
        self._history.pop(run_id, None)

    # ==================================================================
    # 内部：分级与降级
    # ==================================================================
    def _signal_for(self, count: int) -> LoopSignal:
        """§32 的逐级升级：不是发现一次重复就停，而是先警告、再降级、最后才停。

        阈值全部来自 ``config.loop``，运维可以在 YAML 里调而不必改代码。
        """
        policy = self.config.loop
        if count >= policy.duplicate_stop:
            return LoopSignal.STOP
        if count >= policy.duplicate_degrade:
            return LoopSignal.DEGRADED
        if count >= policy.duplicate_warn:
            return LoopSignal.WARNING
        return LoopSignal.OK

    @staticmethod
    def _degradation(tool_name: str, arguments: dict, signal: LoopSignal) -> dict:
        """§33 的可执行降级建议 —— 给出「接下来具体改什么」，而不是一句「请优化」。

        这些建议必须能被机器直接执行，否则降级仍然要靠人：
        改检索模式、砍 ``top_k``、停止测试、把 SQL 变成只读……都是可落地的开关。
        """
        if signal is LoopSignal.OK:
            return {}

        # §33：知识检索陷入循环 -> 换关键词模式并砍半 top_k（缩小搜索空间，跳出同一批结果）
        if tool_name.startswith("search"):
            return {
                "mode": "keyword",
                "top_k": max(1, _safe_int(arguments.get("top_k"), 5) // 2),
                "note": "检索结果没有带来新信息，改用关键词检索并降低 top_k",
            }

        # §33：测试执行循环 -> 直接 Stop（测试是幂等的，但循环说明用例/修复策略有问题）
        if tool_name == "run_test":
            return {
                "action": "stop",
                "note": "测试执行循环 -> Stop",
            }

        # §33：SQL 类 -> 收成只读，避免循环里的写操作反复产生副作用
        lowered = tool_name.lower()
        if "sql" in lowered or "query" in lowered or lowered.startswith("database"):
            return {
                "readonly": True,
                "note": "疑似 SQL 反复执行，降级为只读连接",
            }

        # 兜底：不猜业务语义，只给平台一定能做到的两种收敛手段
        return {
            "action": "fallback",
            "note": f"{tool_name} 重复执行，改用 fallback_tool 或复用上次结果",
        }

    @staticmethod
    def _reason(
        *,
        signal: LoopSignal,
        count: int,
        tool_name: str,
        step_id: str,
        window: float,
        degradation: dict,
    ) -> str:
        if signal is LoopSignal.OK:
            return (
                f"{tool_name} 在步骤 {step_id} 于 {window:.0f}s 窗口内第 {count} 次调用，"
                f"未达重复阈值 -> 正常放行"
            )
        tail = ""
        if degradation:
            tail = f"；建议降级：{degradation.get('note', degradation)}"
        level = {
            LoopSignal.WARNING: "达到 WARNING 阈值",
            LoopSignal.DEGRADED: "达到 DEGRADED 阈值",
            LoopSignal.STOP: "达到 STOP 阈值",
        }[signal]
        return (
            f"{tool_name} 在步骤 {step_id} 于 {window:.0f}s 窗口内以相同参数连续执行 "
            f"{count} 次（§30 四同：同 Tool + 同参数 + 同图步 + 短窗口），"
            f"{level} -> {signal.value}{tail}"
        )


def _safe_int(value: Any, default: int) -> int:
    """宽松取整：LLM 给的参数可能是 ``"5"`` 甚至是 ``"five"``，绝不能因此抛异常。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
