"""DAG 循环检测 —— 说明书 §31 DAG Cycle Detection / §32 分级策略。

重复 vs 循环：两种不同的病
--------------------------
:mod:`app.loop.duplicate` 看的是**同一个 Tool 被同一组参数反复调用**（点上的重复）；
本模块看的是**执行路径本身绕回原点**（链上的环）::

    §31 的例子
    execution_path = ["search", "analyze", "execute", "search", "analyze"]
                     └────── 一轮 ──────┘      └─ 又绕回来了，环长 3

Agent 在 DAG 里绕圈时，每一步的**参数可能都不一样**（比如 ``search`` 的
``query`` 每次都换），因此重复检测抓不到它 —— 但它同样会烧掉预算、永远不收敛。
两个检测器必须同时装：一个盯点，一个盯线。

检测算法
--------
把执行路径当作**节点序列**，找「最早被重访的节点」，它就是环的入口::

    path[i] 在 path[j] (j > i) 处再次出现  ->  环 = path[i:j]，环长 = j - i

这个定义同时覆盖说明书要求识别的两种形态：

* ``A -> B -> A -> B``：最早重访是第二个 ``A``（i=0, j=2）-> 环长 2
* ``A -> A -> A``      ：最早重访是第二个 ``A``（i=0, j=1）-> 环长 1

「走了几圈」则按**周期对齐**来数：入口节点是否在 ``i + m*环长`` 处回归。
之所以只比入口节点、而不做严格的整块比对，是因为 Agent 常会「抄近路」——
``search -> analyze -> execute`` 的环里插一次 ``search -> analyze``，
整块比对会在这一处断掉，但环其实还在继续转。只比入口节点对这种噪声免疫。

截断的取舍（**已知会让长周期漏检**）
------------------------------------
执行路径超过 ``config.loop.max_execution_path`` 时，**截断保留最近的部分**。
不截断的话，一个跑了几小时的长任务会把整条路径永久留在内存里（无界增长）。
代价是刻意的：长度超过保留长度一半的周期再也拼不出完整的两圈，会漏检。
这是一个明确的取舍 —— **宁可漏掉超长周期，也不能让检测器自己变成内存泄漏。**
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import AppConfig
from ..domain.enums import LoopSignal
from ..infra.clock import Clock, SystemClock  # noqa: F401  (Clock 用于类型标注与可读性)


@dataclass
class CycleVerdict:
    """一次循环检测的结论。

    :param signal: §32 分级信号（OK / WARNING / DEGRADED / STOP）
    :param cycle_length: 环长（节点个数）；0 表示当前路径上没有环
    :param repeated_nodes: 环上的节点序列，如 ``["search", "analyze", "execute"]``
    :param reason: 可直接读的中文结论
    :param degradation: §33 的可执行降级建议（空 dict 表示无需降级）
    :param laps: 环已经转了几圈（入口节点在周期对齐位置回归的次数）
    """

    signal: LoopSignal
    cycle_length: int
    repeated_nodes: list[str]
    reason: str
    degradation: dict = field(default_factory=dict)
    laps: int = 0

    @property
    def should_stop(self) -> bool:
        return self.signal is LoopSignal.STOP

    def to_dict(self) -> dict:
        return {
            "signal": self.signal.value,
            "cycle_length": self.cycle_length,
            "repeated_nodes": list(self.repeated_nodes),
            "laps": self.laps,
            "reason": self.reason,
            "degradation": self.degradation,
        }


class CycleDetector:
    """§31 DAG 循环检测器。

    :param config: 平台配置，阈值取自 ``config.loop``
    :param clock: 可注入时钟（保留给「按时间衰减环信号」这类扩展；
        本实现只按路径计数分级，因此时钟不参与判定）
    """

    def __init__(self, config: AppConfig, *, clock: Optional[Clock] = None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self._paths: dict[str, list[str]] = {}

    # ==================================================================
    # 观测
    # ==================================================================
    def observe(self, *, run_id: str, node: str) -> CycleVerdict:
        """观测一个已经走过的图节点，返回当前的循环判定（§31）。

        每进入一个节点就调用一次 —— 环只有在节点**真的走回来**的那一刻
        才存在，因此检测必须发生在路径推进的当下，而不是事后回放。
        """
        path = self._paths.setdefault(run_id, [])
        path.append(node)
        self._truncate(path)

        detected = self._detect_cycle(path)
        if detected is None:
            return CycleVerdict(
                signal=LoopSignal.OK,
                cycle_length=0,
                repeated_nodes=[],
                reason=f"节点 {node} 首次执行，当前路径长度 {len(path)}，未发现环",
                degradation={},
                laps=0,
            )

        cycle_length, repeated_nodes, laps = detected
        signal = self._signal_for(laps)
        degradation = self._degradation(signal, repeated_nodes)
        return CycleVerdict(
            signal=signal,
            cycle_length=cycle_length,
            repeated_nodes=list(repeated_nodes),
            reason=self._reason(
                signal=signal,
                node=node,
                cycle_length=cycle_length,
                repeated_nodes=repeated_nodes,
                laps=laps,
                degradation=degradation,
            ),
            degradation=degradation,
            laps=laps,
        )

    def path(self, run_id: str) -> list[str]:
        """当前保留的执行路径（§31 ``execution_path``）—— 排障时看「它到底绕去哪儿了」。"""
        return list(self._paths.get(run_id, []))

    def reset(self, run_id: str) -> None:
        """清空某个 run 的路径（新一轮任务 / 人工介入重新放行后调用）。"""
        self._paths.pop(run_id, None)

    # ==================================================================
    # 内部：算法
    # ==================================================================
    def _truncate(self, path: list[str]) -> None:
        """超长路径保留最近的 ``max_execution_path`` 个节点（取舍见模块 docstring）。"""
        limit = self.config.loop.max_execution_path
        if limit > 0 and len(path) > limit:
            del path[: len(path) - limit]

    @staticmethod
    def _detect_cycle(path: list[str]) -> Optional[tuple[int, list[str], int]]:
        """找环 —— 返回 ``(环长, 环上节点, 圈数)``；无环返回 ``None``。

        步骤：

        1. 顺序扫描，第一个「之前出现过」的节点就是**环的入口**（最早重访点）。
           取最早的那个（而不是最近的），是为了让环的边界稳定：
           同一个环不会随着路径继续延伸而不断变长变短。
        2. 环 = ``path[i:j]``，环长 = ``j - i``。
        3. 圈数 = 入口节点在 ``i + m*环长`` 处回归的次数（末尾残缺块也算一次回归）。
        """
        first_seen: dict[str, int] = {}
        entry: Optional[tuple[int, int]] = None
        for index, node in enumerate(path):
            start = first_seen.get(node)
            if start is not None:
                entry = (start, index)
                break
            first_seen[node] = index

        if entry is None:
            return None

        start, revisit = entry
        cycle_length = revisit - start
        repeated_nodes = path[start:revisit]

        laps = 0
        step = 0
        while start + step * cycle_length < len(path):
            if path[start + step * cycle_length] != path[start]:
                break
            laps += 1
            step += 1

        return cycle_length, repeated_nodes, laps

    def _signal_for(self, laps: int) -> LoopSignal:
        """§32 分级：按「重复块（环）出现的次数」映射到 WARNING / DEGRADED / STOP。"""
        policy = self.config.loop
        if laps >= policy.cycle_stop:
            return LoopSignal.STOP
        if laps >= policy.cycle_degrade:
            return LoopSignal.DEGRADED
        if laps >= policy.cycle_warn:
            return LoopSignal.WARNING
        return LoopSignal.OK

    @staticmethod
    def _degradation(signal: LoopSignal, repeated_nodes: list[str]) -> dict:
        """§33 的可执行降级建议：给出「怎么把环切开」，而不是一句「请避免循环」。

        环的切法是有限几种，都必须是机器能直接执行的：
        换 fallback、跳过环上的非关键节点、或者干脆停机等人。
        """
        if signal is LoopSignal.OK:
            return {}
        if signal is LoopSignal.WARNING:
            return {
                "action": "observe",
                "cycle_nodes": list(repeated_nodes),
                "note": "环已出现，收紧步数预算并记录环上节点",
            }
        if signal is LoopSignal.DEGRADED:
            return {
                "action": "break_cycle",
                "strategy": "fallback_or_skip",
                "cycle_nodes": list(repeated_nodes),
                "note": f"环 {repeated_nodes} 已重复出现，改用 fallback_tool 或跳过环上的非关键节点",
            }
        return {
            "action": "stop",
            "cycle_nodes": list(repeated_nodes),
            "note": "环反复出现且没有新信息，停止该 run 并交由人工判断",
        }

    @staticmethod
    def _reason(
        *,
        signal: LoopSignal,
        node: str,
        cycle_length: int,
        repeated_nodes: list[str],
        laps: int,
        degradation: dict,
    ) -> str:
        tail = ""
        if degradation:
            tail = f"；建议降级：{degradation.get('note', degradation)}"
        return (
            f"节点 {node} 使执行路径回到已访问节点，识别出长度 {cycle_length} 的环 "
            f"{repeated_nodes}，已重复 {laps} 圈 -> {signal.value}"
            f"（§31 execution_path 绕回原点，§32 按重复次数分级{tail}）"
        )
