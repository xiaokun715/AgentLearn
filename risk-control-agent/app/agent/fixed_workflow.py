"""固定调查工作流 —— **对照组**。

它做的事和 :mod:`app.agent.investigator` 一样，唯一的区别是：
**它不问「上一步查出了什么」，只按清单往下走。**

    账号画像 → 设备关联 → 交易明细 → 历史案件 → 出报告

为什么要有这个文件：本 Demo 的核心主张是「Agent 的价值在于动态路径」。
一个没有对照组的主张是无法验证的 —— 所以这里把「普通工作流」也实现出来，
用同一批案件跑，比较步数、结论、以及**结论对不对**。

它暴露出来的问题很具体（见 ``paths`` 场景的实测）：

* **把「查不到」当成「没问题」** —— ``query_device_graph`` 因为数据源故障
  返回 INCONCLUSIVE，固定流程照样往下走，最后输出「未发现异常」。
  而正确结论应该是「证据不足，转人工」。这是**最危险的一类错误**：
  它安静、可解释、而且看起来很正常；
* **多跑无用的步骤** —— 已经确认是设备农场之后还要去查交易明细；
* **顺序由人预先定义，而不是由证据决定** —— 案件形态一变，清单就该变，
  但你没法穷举所有形态（这正是 §4.4 的出发点）。
"""
from __future__ import annotations

from ..domain.enums import (
    Disposition,
    HypothesisStatus,
    InvestigationOutcome,
    Observation,
)
from ..domain.models import Alert, InvestigationReport, ToolCall
from ..gateway.tool_gateway import ToolGateway, build_scope
from ..sim import RiskWorld
from ..tools import investigation as tool_impl

#: 人工预先定义好的调查清单
FIXED_STEPS = [
    "query_account_profile",
    "query_device_graph",
    "query_transaction_history",
    "search_similar_cases",
]


class FixedWorkflowRunner:
    """按固定清单执行调查。"""

    def __init__(self, world: RiskWorld, gateway: ToolGateway) -> None:
        self.world = world
        self.gateway = gateway

    # ------------------------------------------------------------------
    def investigate(self, alert: Alert) -> tuple[InvestigationReport, dict]:
        scope = build_scope(self.world, alert.entity_id)
        device = self._device_of(alert.entity_id)
        positives = 0
        inconclusive = 0
        path: list[str] = []
        evidence_ids: list[str] = []
        denied: list[str] = []
        notes: list[str] = []

        for name in FIXED_STEPS:
            arguments = self._arguments_for(name, alert, device)
            verdict = self.gateway.check(ToolCall(tool=name, arguments=arguments), scope=scope)
            if not verdict.allowed:
                denied.append(name)
                notes.append(f"{name} 被拦下：{verdict.message}")
                continue
            result = tool_impl.resolve(name)(self.world, arguments)
            path.append(name)
            evidence_ids.extend(item.evidence_id for item in result.evidence)
            # **关键差异**：INCONCLUSIVE 在这里被当成「没查出问题」
            if result.observation is Observation.ANOMALY:
                positives += 1
            elif result.observation is Observation.INCONCLUSIVE:
                inconclusive += 1
                notes.append(f"{name} 无结论（数据源不可用）—— 固定流程按「未发现异常」继续")
            notes.append(f"{name} → {result.observation.value}")

        outcome, disposition, confidence, summary = self._conclude(positives, inconclusive)
        report = InvestigationReport(
            alert_id=alert.alert_id,
            entity_id=alert.entity_id,
            outcome=outcome,
            disposition=disposition,
            executed=False,
            summary=summary,
            hypotheses=[{
                "key": "FIXED_PATH",
                "description": "按预定义清单执行，未维护假设状态",
                "status": HypothesisStatus.UNDECIDED.value,
                "weight": 0.0,
                "support": positives,
                "refute": 0,
                "sources": list(path),
            }],
            evidence_ids=evidence_ids,
            investigation_path=path,
            missing_evidence=[],
            confidence=confidence,
            tool_calls=len(path),
            denied_calls=denied,
            notes=notes,
        )
        return report, {"positives": positives, "inconclusive": inconclusive}

    # ------------------------------------------------------------------
    @staticmethod
    def _conclude(
        positives: int, inconclusive: int
    ) -> tuple[InvestigationOutcome, Disposition, float, str]:
        if positives >= 2:
            return (InvestigationOutcome.CONFIRMED_RISK, Disposition.KEEP_BLOCK,
                    min(0.9, 0.5 + 0.15 * positives),
                    f"固定流程命中 {positives} 项异常")
        if positives == 1:
            return (InvestigationOutcome.NEEDS_HUMAN_JUDGEMENT, Disposition.ESCALATE, 0.45,
                    "固定流程只命中 1 项异常，不足以定性")
        # **这里就是问题所在**：不管有几个数据源没查成，都走到这个分支
        return (InvestigationOutcome.LIKELY_FALSE_POSITIVE,
                Disposition.CLOSE_AS_FALSE_POSITIVE, 0.6,
                "固定流程未发现异常，判定为误报")

    # ------------------------------------------------------------------
    @staticmethod
    def _arguments_for(name: str, alert: Alert, device: str) -> dict:
        if name in ("query_device_graph",):
            return {"device_id": device}
        if name == "search_similar_cases":
            return {"keyword": ""}
        return {"user_id": alert.entity_id}

    def _device_of(self, user_id: str) -> str:
        for device_id, device in self.world.devices.items():
            if user_id in device.accounts:
                return device_id
        return ""
