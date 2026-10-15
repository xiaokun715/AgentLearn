"""日志类诊断工具 —— 从日志里拿一手证据（说明书 §6.1 动作池）。

两个动作刻意分开：

- ``analyze_logs``   解析后给出**结构化发现**（错误事件、时间线）—— 快，但经过了加工；
- ``retrieve_raw_log`` 拉未过滤的原始日志 —— 慢，但那是「原始证据」本身。

说明书 §3.1 要求原始证据与推断分开保存，所以这两个动作产出的证据层级也不同：
后者落 RAW（带 ``artifact://`` 引用），前者落 OBSERVED。
"""
from __future__ import annotations

from ..domain.enums import EvidenceKind, Observation
from ..domain.models import EvidenceDraft, PlanNode, ToolObservation
from .base import ToolContext, ToolRegistry, observation_outcome


def analyze_logs(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """解析日志引用，提取错误事件与时间线。"""
    world = ctx.platform.world

    if world.sparse or not world.context.raw_logs:
        why = "日志已被轮转，引用不可用" if world.sparse else "日志引用为空"
        return ToolObservation(
            skill="analyze_logs",
            outcome=Observation.INCONCLUSIVE,
            summary=f"日志不可用：{why}",
            payload={"reason": why},
            evidence=[
                EvidenceDraft(
                    kind=EvidenceKind.OBSERVED,
                    claim=f"日志分析无法给出结论（{why}）",
                    payload={"reason": why},
                )
            ],
        )

    findings = world.log_findings
    outcome = observation_outcome(bool(findings))
    drafts = [
        EvidenceDraft(
            kind=EvidenceKind.RAW,
            claim=f"原始日志第 {index + 1} 行：{line}",
            payload={"line": line, "index": index},
            ref=ctx.store_artifact(f"logs/{world.name}_{index}.log", line),
        )
        for index, line in enumerate(world.context.raw_logs[:4])
    ]
    drafts.append(
        EvidenceDraft(
            kind=EvidenceKind.OBSERVED,
            claim=(
                f"日志分析发现 {len(findings)} 条异常事件"
                if findings else "日志分析未发现异常事件"
            ),
            payload={"findings": findings},
        )
    )
    return ToolObservation(
        skill="analyze_logs",
        outcome=outcome,
        summary=(
            "；".join(findings) if findings else "日志里没有错误事件与异常时间线"
        ),
        payload={"findings": findings, "log_lines": len(world.context.raw_logs)},
        evidence=drafts,
    )


def retrieve_raw_log(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """拉取未过滤的原始日志。**这是「原始证据」本身，不做任何判断。**"""
    world = ctx.platform.world
    logs = world.context.raw_logs
    if not logs:
        return ToolObservation(
            skill="retrieve_raw_log",
            outcome=Observation.INCONCLUSIVE,
            summary="对象存储里没有该任务的日志对象",
            payload={"reason": "log object not found"},
        )

    markers = [code for code in world.context.error_codes]
    drafts = [
        EvidenceDraft(
            kind=EvidenceKind.RAW,
            claim=f"原始日志：{line}",
            payload={"line": line},
            ref=ctx.store_artifact(f"raw/{world.name}_{index}.log", line),
        )
        for index, line in enumerate(logs)
    ]
    has_error_lines = any(
        token in line.upper()
        for line in logs
        for token in ("ERROR", "FATAL", "ABORT", "WARN")
    )
    return ToolObservation(
        skill="retrieve_raw_log",
        outcome=observation_outcome(has_error_lines),
        summary=(
            f"取回 {len(logs)} 行原始日志，其中包含错误/告警行"
            if has_error_lines else f"取回 {len(logs)} 行原始日志，全部为常规信息"
        ),
        payload={"lines": len(logs), "error_codes": markers},
        ref=drafts[0].ref if drafts else None,
        evidence=drafts,
    )


def register_all(registry: ToolRegistry) -> None:
    registry.add("analyze_logs", analyze_logs)
    registry.add("retrieve_raw_log", retrieve_raw_log)
