"""配置与用例定义类诊断工具（说明书 §6.1 动作池）。

``inspect_config`` 与 ``validate_case_definition`` 经常被混为一谈，
但它们在排障里指向两个完全不同的假设：

- 配置异常 H_CONFIG：**怎么跑**的参数不对（周期、模式、开关）；
- 用例异常 H_CASE：**判什么算过**的定义不对（预期结果与需求约束不符）。

分开实现，才能让信息增益算得准 —— 合成一个「检查配置和用例」的动作，
它对两个假设的似然就混在一起了，区分力必然下降。
"""
from __future__ import annotations

from ..domain.enums import EvidenceKind, Observation
from ..domain.models import EvidenceDraft, PlanNode, ToolObservation
from .base import ToolContext, ToolRegistry, observation_outcome


def inspect_config(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """比对测试配置与需求约束，输出配置差异。"""
    world = ctx.platform.world
    config = world.context.test_config

    if world.sparse or not config:
        return ToolObservation(
            skill="inspect_config",
            outcome=Observation.INCONCLUSIVE,
            summary="配置读取失败，无法比对（配置服务返回空）",
            payload={"reason": "config unavailable"},
        )

    findings = world.config_findings
    drafts = [
        EvidenceDraft(
            kind=EvidenceKind.RAW,
            claim=f"测试配置项 {key}={value!r}",
            payload={"key": key, "value": value},
        )
        for key, value in config.items()
    ]
    drafts.append(
        EvidenceDraft(
            kind=EvidenceKind.OBSERVED,
            claim=(
                f"配置与需求约束存在 {len(findings)} 处不一致"
                if findings else "配置项与需求约束一致"
            ),
            payload={"findings": findings},
        )
    )
    return ToolObservation(
        skill="inspect_config",
        outcome=observation_outcome(bool(findings)),
        summary="；".join(findings) if findings else "配置项与需求约束一致，未发现差异",
        payload={"config": config, "findings": findings},
        evidence=drafts,
    )


def validate_case_definition(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """校验用例的预期结果是否与需求约束一致。"""
    world = ctx.platform.world

    if world.sparse:
        return ToolObservation(
            skill="validate_case_definition",
            outcome=Observation.INCONCLUSIVE,
            summary="用例版本服务不可用，无法校验定义",
            payload={"reason": "case registry unavailable"},
        )

    findings = world.case_findings
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
                f"用例预期结果定义存在 {len(findings)} 处问题"
                if findings else "用例预期结果与需求约束一致"
            ),
            payload={"findings": findings},
        )
    )
    return ToolObservation(
        skill="validate_case_definition",
        outcome=observation_outcome(bool(findings)),
        summary="；".join(findings) if findings else "用例定义与需求约束一致",
        payload={"findings": findings,
                 "case_id": world.context.test_case_id},
        evidence=drafts,
    )


def register_all(registry: ToolRegistry) -> None:
    registry.add("inspect_config", inspect_config)
    registry.add("validate_case_definition", validate_case_definition)
