"""执行轨迹分析 —— 回答「故障根因到底在哪个节点」（说明书 §12 第一问）。

说明书原话：

> 失败节点不一定是根因节点。例如，用例执行失败可能是因为上游配置生成错误，
> 或者环境准备节点虽然返回成功，但实际上没有正确初始化环境。

因此本模块不复用「谁报错谁是凶手」的朴素做法，而是把轨迹当**依赖图**看待，
区分三类节点：

==================== ==========================================================
类型                  判据
==================== ==========================================================
PHENOMENON（现象）    真正报错的节点 —— 它通常是受害者，不是原因
SUSPECT（疑似根因）   声称成功、却在结构性信号上暴露「没真正做到」的上游节点
NOISE（噪声）         与本次故障无关的异常（例如重试成功的历史失败）
==================== ==========================================================

「声称成功但没做到」的判据必须是**结构性**的，不能是「模型觉得不对」：
本模块只看 metadata 里由上游系统写入的事实字段（``verified`` / ``output_ref`` /
``result_complete`` / 下游的 ``precondition_violation``）。
"""
from __future__ import annotations

from ..domain.models import FailureContext, TraceAnalysis, TraceEvent


class TraceAnalyzer:
    """轨迹分析器 —— 纯函数式，不依赖模型。"""

    def analyze(self, ctx: FailureContext) -> TraceAnalysis:
        events = ctx.execution_trace
        analysis = TraceAnalysis()
        if not events:
            analysis.notes.append("轨迹为空 —— 无法还原失败过程，只能依赖日志与配置证据")
            return analysis

        index = {event.event_id: event for event in events}
        children: dict[str, list[str]] = {event.event_id: [] for event in events}
        parents: dict[str, str | None] = {}
        for event in events:
            parents[event.event_id] = event.parent_event_id
            if event.parent_event_id and event.parent_event_id in children:
                children[event.parent_event_id].append(event.event_id)

        analysis.dependency_edges = [
            (event.parent_event_id, event.event_id)
            for event in events
            if event.parent_event_id and event.parent_event_id in index
        ]

        # ---- 1. 现象节点：最后一个真正失败的事件 --------------------------
        phenomenon = next((event for event in reversed(events) if event.failed), None)
        if phenomenon is None:
            analysis.notes.append("轨迹里没有失败事件 —— 失败可能发生在业务阶段之外（如断言比对）")
        else:
            analysis.phenomenon_node = phenomenon.event_id
            analysis.notes.append(
                f"现象节点 {phenomenon.event_id}（{phenomenon.skill or phenomenon.agent}）"
                f" 报错 {phenomenon.error_code or phenomenon.status}"
            )

        # ---- 2. 疑似根因节点：从现象节点向上游回溯 -------------------------
        suspects: list[str] = []
        if phenomenon is not None:
            for ancestor_id in self._ancestors(phenomenon.event_id, parents):
                ancestor = index[ancestor_id]
                signals = self._false_success_signals(ancestor, children, index)
                if signals:
                    suspects.append(ancestor_id)
                    analysis.anomalies.append(
                        {
                            "event_id": ancestor_id,
                            "skill": ancestor.skill or ancestor.agent,
                            "kind": "FALSE_SUCCESS",
                            "signals": signals,
                        }
                    )
            # 上游在前 —— 越早的「假成功」越可能是根因
            suspects.reverse()

        analysis.suspect_nodes = suspects

        # ---- 3. 噪声节点：不在现象节点祖先链上的失败 -----------------------
        if phenomenon is not None:
            chain = set(self._ancestors(phenomenon.event_id, parents)) | {phenomenon.event_id}
            for event in events:
                if event.failed and event.event_id not in chain:
                    analysis.anomalies.append(
                        {
                            "event_id": event.event_id,
                            "skill": event.skill or event.agent,
                            "kind": "NOISE",
                            "signals": ["该失败不在现象节点的依赖链上（可能是重试前的历史失败）"],
                        }
                    )

        # ---- 4. 结论性提示 -------------------------------------------------
        if phenomenon is not None and suspects:
            analysis.notes.append(
                "现象节点不是根因节点：上游存在声称成功但未真正生效的节点"
            )
        elif phenomenon is not None:
            analysis.notes.append("未发现上游假成功节点，现象节点可视为根因候选")
        return analysis

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _ancestors(event_id: str, parents: dict[str, str | None]) -> list[str]:
        """自底向上收集祖先（含所有层级），保持「越靠近现象越靠前」。"""
        result: list[str] = []
        cursor = parents.get(event_id)
        guard = 0
        while cursor and guard < 64:      # guard：轨迹数据坏了也不要死循环
            result.append(cursor)
            cursor = parents.get(cursor)
            guard += 1
        return result

    @staticmethod
    def _false_success_signals(
        event: TraceEvent,
        children: dict[str, list[str]],
        index: dict[str, TraceEvent],
    ) -> list[str]:
        """返回该事件的「假成功」信号；空列表表示它是一次可信的成功。"""
        if not event.claimed_success:
            return []

        signals: list[str] = []
        meta = event.metadata or {}

        # 信号 a：上游系统明确标注「未经校验」
        if meta.get("verified") is False:
            signals.append("节点自称成功，但 verified=false（未做结果校验）")

        # 信号 b：本该产出结果，却没有 output_ref
        if meta.get("expects_output", False) and not event.output_ref:
            signals.append("节点自称成功，但没有产出 output_ref（结果缺失）")

        # 信号 c：自称完成，但 result_complete=false（部分执行）
        if meta.get("result_complete") is False:
            signals.append("节点自称成功，但 result_complete=false（只做了一部分）")

        # 信号 d：下游节点记录了「前置条件不满足」，且指向这个上游
        for child_id in children.get(event.event_id, []):
            child = index.get(child_id)
            if child is None:
                continue
            violation = (child.metadata or {}).get("precondition_violation")
            if violation and (
                violation == event.skill
                or violation == event.event_id
                or violation == event.tool
            ):
                signals.append(
                    f"下游节点 {child_id} 报告前置条件不满足：{violation}"
                )

        return signals


def summarize(analysis: TraceAnalysis) -> str:
    """把分析结果压成一行，便于终端展示。"""
    if analysis.phenomenon_is_root:
        return "现象节点即根因候选（未发现上游假成功）"
    return (
        f"现象节点 {analysis.phenomenon_node} 只是受害者；"
        f"疑似根因节点 {analysis.suspect_nodes}"
    )
