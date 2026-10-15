"""测试/工具执行类诊断与恢复工具（说明书 §6.1 动作池）。

这里有一个容易被忽略的动作：``retrieve_failure_cases``。
它的似然表对所有假设都是 ``0.30 / 0.70`` —— **信息增益恒等于 0**。
这不是偷懒，而是刻意的示范：历史案例对排障很有价值（提供先验、提供先例），
但它的价值**不在于区分根因**。规划器永远不会为了「缩小不确定性」选它，
这正好印证说明书 §5.3 的那句「比只能重复确认已知事实的检查更有价值」。
"""
from __future__ import annotations

from ..domain.enums import EvidenceKind, Observation
from ..domain.models import EvidenceDraft, PlanNode, ToolObservation
from .base import ToolContext, ToolRegistry, observation_outcome


def inspect_tool_records(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """查询测试工具执行记录：返回码、超时、采集状态。"""
    world = ctx.platform.world

    if world.sparse:
        return ToolObservation(
            skill="inspect_tool_records",
            outcome=Observation.INCONCLUSIVE,
            summary="工具执行记录接口超时，取不到返回码",
            payload={"reason": "tool records service timeout"},
        )

    findings = world.tool_record_findings
    drafts = [
        EvidenceDraft(
            kind=EvidenceKind.OBSERVED,
            claim=claim,
            payload={"finding": claim},
        )
        for claim in findings
    ]
    drafts.append(
        EvidenceDraft(
            kind=EvidenceKind.OBSERVED,
            claim=(
                f"工具执行记录里有 {len(findings)} 项异常"
                if findings else "工具执行记录正常（返回码 0，无超时，采集完整）"
            ),
            payload={"findings": findings},
        )
    )
    return ToolObservation(
        skill="inspect_tool_records",
        outcome=observation_outcome(bool(findings)),
        summary="；".join(findings) if findings else "工具执行记录正常，采集完整",
        payload={"findings": findings},
        evidence=drafts,
    )


def retrieve_failure_cases(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """按故障指纹检索历史案例。

    返回的 ``outcome`` 恒为 ``NORMAL`` —— 它的作用是给排障人员与经验库提供上下文，
    不是给假设打分（见模块 docstring）。
    """
    world = ctx.platform.world
    cases = world.history_cases
    return ToolObservation(
        skill="retrieve_failure_cases",
        outcome=Observation.NORMAL,
        summary=(
            f"检索到 {len(cases)} 条历史案例" if cases else "没有检索到相似的历史案例"
        ),
        payload={"cases": cases},
        evidence=[
            EvidenceDraft(
                kind=EvidenceKind.OBSERVED,
                claim=f"历史案例检索命中 {len(cases)} 条",
                payload={"cases": cases},
            )
        ],
    )


def rerun_test(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """恢复动作（R2）：在恢复后的环境上重跑测试用例。

    **复测通过不能单独证明根因已经确定**（说明书 §7.2）。所以这个工具只返回
    「复测结果」这个事实，不返回「问题已解决」这种结论 —— 结论由
    ``recovery/controller.py`` 结合前后快照一起下。
    """
    result = ctx.platform.rerun()
    passed = result["result"] == "PASSED"
    return ToolObservation(
        skill="rerun_test",
        outcome=Observation.NORMAL if passed else Observation.ANOMALY,
        summary=f"复测{result['result']}：{result['note']}",
        payload={"rerun": result},
        evidence=[
            EvidenceDraft(
                kind=EvidenceKind.OBSERVED,
                claim=f"复测结果：{result['result']}",
                payload=dict(result),
            )
        ],
    )


def override_test_expectation(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """R4：修改测试预期结果。

    这个工具体现了说明书 §9 的那句提醒 —— 「如果系统通过修改测试预期结果来提高
    通过率，就会出现指标上升但测试质量下降的问题」。

    它在 (a) 校验器的 R4 禁令、(b) 默认权限集 两处都被挡住，
    正常情况下**永远不会被执行**。实现它只是为了说明「为什么它必须存在却不可达」。
    """
    return ToolObservation(
        skill="override_test_expectation",
        outcome=Observation.ERROR,
        summary="R4 动作不允许自动执行：修改测试预期会掩盖真实故障",
        payload={"blocked": True},
    )


def create_defect_report(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """恢复动作（R1）：基于验证后的结论创建缺陷单。"""
    summary = (node.inputs or {}).get("summary", "排障结论生成的缺陷单")
    defect = ctx.platform.create_defect(summary)
    return ToolObservation(
        skill="create_defect_report",
        outcome=Observation.NORMAL,
        summary=f"已创建缺陷单 {defect['defect_id']}",
        payload={"defect": defect},
        evidence=[
            EvidenceDraft(
                kind=EvidenceKind.VERIFIED,
                claim=f"缺陷单 {defect['defect_id']} 已创建：{summary}",
                payload=dict(defect),
            )
        ],
    )


def register_all(registry: ToolRegistry) -> None:
    registry.add("inspect_tool_records", inspect_tool_records)
    registry.add("retrieve_failure_cases", retrieve_failure_cases)
    registry.add("rerun_test", rerun_test)
    registry.add("override_test_expectation", override_test_expectation)
    registry.add("create_defect_report", create_defect_report)
