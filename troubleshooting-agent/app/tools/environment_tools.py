"""环境类工具 —— 只读探针 + 三个分级的恢复动作（说明书 §7.1）。

**诊断与恢复在这里被刻意放在同一个模块里，但用不同的 ``kind`` 区分**：
``check_environment_state`` 是 ``diagnostic``，其余三个是 ``recovery``。
校验器会拒绝任何把 recovery 节点塞进诊断计划的计划（见 §6.3 第 8 条），
所以「先诊断、后恢复」不是靠调用方自觉，而是靠校验器强制的。

参数来源：这些工具通过 ``node.inputs`` 拿参数，而不是去读全局状态 ——
计划是数据，工具只消费计划给它的东西。
"""
from __future__ import annotations

from ..domain.enums import EvidenceKind, Observation
from ..domain.models import EvidenceDraft, PlanNode, ToolObservation
from .base import ToolContext, ToolRegistry

#: 环境探针的三种结论 → 观测结果
_PROBE_MAP = {
    "READY": Observation.NORMAL,
    "NOT_READY": Observation.ANOMALY,
    "INCONCLUSIVE": Observation.INCONCLUSIVE,
}


def _probe(ctx: ToolContext, skill: str) -> ToolObservation:
    probe = ctx.platform.probe()
    outcome = _PROBE_MAP.get(probe["status"], Observation.INCONCLUSIVE)
    drafts = [
        EvidenceDraft(
            kind=EvidenceKind.RAW,
            claim=f"环境探针原始返回：{detail}",
            payload={"detail": detail, "status": probe["status"]},
        )
        for detail in probe["details"]
    ]
    drafts.append(
        EvidenceDraft(
            kind=EvidenceKind.OBSERVED,
            claim=f"环境状态判定为 {probe['status']}",
            payload=dict(probe),
        )
    )
    return ToolObservation(
        skill=skill,
        outcome=outcome,
        summary=f"环境探针返回 {probe['status']}：" + "；".join(probe["details"]),
        payload=dict(probe),
        evidence=drafts,
    )


def check_environment_state(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """诊断动作（R0，只读）：检查设备、资源与初始化状态。"""
    return _probe(ctx, "check_environment_state")


def refresh_environment_state(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """恢复动作（R1）：无副作用的状态刷新。

    R1 的价值是「不改世界再确认一次」—— 很多所谓的环境异常其实是**观测缓存过期**。
    所以恢复升级路径的第一步永远是它，而不是直接重启设备。
    """
    observation = _probe(ctx, "refresh_environment_state")
    observation.payload["recovery"] = {"action": "refresh", "mutated": False}
    return observation


def reallocate_environment(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """恢复动作（R2）：重新申请一台符合条件的环境并重新初始化。

    R2 会**真的改变环境**，所以 §7.1 要求检查配额、成本和幂等性。
    这里把它做成幂等的：对同一个平台重复调用不会越申请越多。
    """
    record = ctx.platform.apply_recovery("reallocate_environment")
    ready = record["env_ready"]
    return ToolObservation(
        skill="reallocate_environment",
        outcome=Observation.NORMAL if ready else Observation.ANOMALY,
        summary=record["note"],
        payload={"recovery": record},
        evidence=[
            EvidenceDraft(
                kind=EvidenceKind.OBSERVED,
                claim=f"重新申请环境已执行，环境就绪={ready}",
                payload=dict(record),
            )
        ],
    )


def restart_device(ctx: ToolContext, node: PlanNode) -> ToolObservation:
    """恢复动作（R3）：重启被测设备。

    需要 ``recovery:restart-device`` 权限 —— 该权限**不在**默认权限集里，
    必须由人工审批临时授予（``domain/policy.py::APPROVAL_GRANTS``）。
    校验器在看它的时候，先看 R3 的审批矩阵，再看权限集。
    """
    record = ctx.platform.apply_recovery("restart_device")
    ready = record["env_ready"]
    return ToolObservation(
        skill="restart_device",
        outcome=Observation.NORMAL if ready else Observation.ANOMALY,
        summary=record["note"],
        payload={"recovery": record},
        evidence=[
            EvidenceDraft(
                kind=EvidenceKind.OBSERVED,
                claim=f"设备重启已执行，环境就绪={ready}",
                payload=dict(record),
            )
        ],
    )


def register_all(registry: ToolRegistry) -> None:
    registry.add("check_environment_state", check_environment_state)
    registry.add("refresh_environment_state", refresh_environment_state)
    registry.add("reallocate_environment", reallocate_environment)
    registry.add("restart_device", restart_device)
