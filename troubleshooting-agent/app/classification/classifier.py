"""故障分类 —— 排障的第一个决策点（说明书 §4）。

说明书 §4.2 的流程图要求「规则优先 + LLM 补充」，并特别提醒：

> 这里不应简单地将「规则没有命中」理解为「规则分类器判断失败」。
> 如果规则无法确定分类，应允许返回 ``UNKNOWN``，再让 LLM 基于有限证据提出候选类别。

本模块把这层意思落成两条硬约束：

1. **规则只在无歧义时开火**。例如「日志里直接出现 ``ENV_NOT_READY``」可以判环境；
   而「预期与实际不一致」既可能是软件缺陷，也可能是用例写错了 —— 那就不判，
   交给 LLM 出候选类别。宁可 UNKNOWN，也不给一个看似笃定的错类别。
2. **LLM 的输出必须过一次 Schema 校验**。模型返回的字典要能被
   :class:`~app.domain.models.FailureClassification` 构造出来，否则退化成 UNKNOWN。
   这一步挡住了「模型自由发挥出一堆不存在的类别」。

另外记一笔说明书的告诫：``confidence`` 是估计值，**不是校准过的真实概率**，
所以本 Demo 只用它排序和展示，恢复决策一律走风险分级 + 证据门（见 README「关键取舍 3」）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError

from ..domain.enums import FailureCategory
from ..domain.models import FailureClassification, FailureContext, TraceAnalysis

#: 规则判定的输出（仅在**无歧义**时给出）
RuleVerdict = tuple[FailureCategory, list[FailureCategory], float, str, list[str]]


@dataclass(frozen=True)
class ClassificationRule:
    """一条分类规则。

    ``matches`` 只允许读**确定性输入**：错误码、工具返回码、轨迹的结构化字段。
    任何形如「模型觉得像」的判据都不允许出现在这里 —— 那会让规则层失去可测试性。
    """

    name: str
    description: str
    matches: Callable[[FailureContext, TraceAnalysis], RuleVerdict | None]


# ----------------------------------------------------------------------
# 错误码 → 类别的确定性映射表
# ----------------------------------------------------------------------
_ENV_ERROR_CODES = {
    "ENV_NOT_READY", "DEVICE_UNAVAILABLE", "RESOURCE_UNBOUND", "ENV_INIT_FAILED",
}
_TOOL_ERROR_CODES = {
    "TOOL_TIMEOUT", "TOOL_RETURN_CODE", "COLLECT_ABORTED", "TOOL_NOT_FOUND",
}


def _error_codes(ctx: FailureContext) -> set[str]:
    return set(ctx.error_codes)


RULES: list[ClassificationRule] = [
    ClassificationRule(
        name="RULE_ENV_ERROR_CODE",
        description="轨迹里出现明确的环境类错误码",
        matches=lambda ctx, trace: (
            (codes := _error_codes(ctx) & _ENV_ERROR_CODES)
            and (
                FailureCategory.ENVIRONMENT,
                [FailureCategory.TOOL_EXECUTION],
                0.85,
                f"轨迹里的错误码 {sorted(codes)} 直接指向环境未就绪",
                ["check_environment_state", "analyze_logs"],
            )
        )
        or None,
    ),
    ClassificationRule(
        name="RULE_FALSE_SUCCESS_UPSTREAM",
        description="上游节点自称成功但未真正生效，且该节点属于环境准备阶段",
        matches=lambda ctx, trace: (
            (suspects := _suspect_events(ctx, trace))
            and any(
                "env" in (suspect.skill or "").lower() or "environment" in (suspect.skill or "").lower()
                for suspect in suspects
            )
            and (
                FailureCategory.ENVIRONMENT,
                [FailureCategory.ORCHESTRATION],
                0.80,
                "轨迹里存在自称成功但未经校验的环境准备节点",
                ["check_environment_state", "analyze_logs"],
            )
        )
        or None,
    ),
    ClassificationRule(
        name="RULE_TOOL_FAILURE",
        description="轨迹里出现明确的工具类错误码",
        matches=lambda ctx, trace: (
            (codes := _error_codes(ctx) & _TOOL_ERROR_CODES)
            and (
                FailureCategory.TOOL_EXECUTION,
                [],
                0.72,
                f"轨迹里的错误码 {sorted(codes)} 指向工具执行异常",
                ["inspect_tool_records", "analyze_logs"],
            )
        )
        or None,
    ),
    ClassificationRule(
        name="RULE_CONFIG_INVALID",
        description="配置项被上游校验器明确标为非法（不是「可能不对」，是「已判定非法」）",
        matches=lambda ctx, trace: (
            ctx.test_config.get("validation", {}).get("verdict") == "ILLEGAL"
            and (
                FailureCategory.CONFIG,
                [FailureCategory.REQUIREMENT],
                0.88,
                "测试配置被校验器判定为非法取值",
                ["inspect_config", "validate_case_definition"],
            )
        )
        or None,
    ),
]


def _suspect_events(ctx: FailureContext, trace: TraceAnalysis):
    index = {event.event_id: event for event in ctx.execution_trace}
    return [index[sid] for sid in trace.suspect_nodes if sid in index]


class FailureClassifier:
    """规则优先 + LLM 补充的分层分类器。"""

    def __init__(self, llm=None) -> None:
        self.llm = llm

    def classify(
        self,
        ctx: FailureContext,
        trace: TraceAnalysis,
    ) -> FailureClassification:
        # ---- 阶段一：规则 ------------------------------------------------
        for rule in RULES:
            verdict = rule.matches(ctx, trace)
            if verdict is None:
                continue
            primary, secondary, confidence, rationale, next_checks = verdict
            return FailureClassification(
                primary_category=primary,
                secondary_categories=secondary,
                confidence=confidence,
                evidence_ids=self._trace_evidence(ctx, trace),
                next_checks=next_checks,
                decided_by="rule",
                rationale=f"[{rule.name}] {rationale}",
            )

        # ---- 阶段二：规则命不中 → 允许 UNKNOWN，或让 LLM 提候选类别 --------
        return self._classify_by_llm(ctx, trace)

    # ------------------------------------------------------------------
    def _classify_by_llm(
        self,
        ctx: FailureContext,
        trace: TraceAnalysis,
    ) -> FailureClassification:
        evidence_ids = self._trace_evidence(ctx, trace)
        if self.llm is None:
            return FailureClassification(
                primary_category=FailureCategory.UNKNOWN,
                confidence=0.0,
                evidence_ids=evidence_ids,
                decided_by="unknown",
                rationale="规则无法确定分类，且没有可用的 LLM 兜底 -> 判定 UNKNOWN",
            )

        raw = self.llm.classify_failure(ctx, trace)

        # ---- 阶段三：Schema 校验（不是「相信模型返回的字典」）-------------
        try:
            classification = FailureClassification.model_validate(raw)
        except ValidationError as exc:
            return FailureClassification(
                primary_category=FailureCategory.UNKNOWN,
                confidence=0.0,
                evidence_ids=evidence_ids,
                decided_by="unknown",
                rationale=f"LLM 输出未通过 Schema 校验（{exc.error_count()} 处问题）-> 判定 UNKNOWN",
            )

        classification.decided_by = "llm"
        classification.evidence_ids = evidence_ids or classification.evidence_ids
        # 模型给的置信度要打折：说明书明确说它未经校准
        classification.confidence = round(min(classification.confidence, 0.7), 3)
        return classification

    @staticmethod
    def _trace_evidence(ctx: FailureContext, trace: TraceAnalysis) -> list[str]:
        """分类依据的轨迹事件 ID（后续由证据库补上具体证据）。"""
        ids: list[str] = []
        if trace.phenomenon_node:
            ids.append(trace.phenomenon_node)
        ids.extend(trace.suspect_nodes)
        return ids


def explain(classification: FailureClassification) -> str:
    """分类结果的一行说明。"""
    secondary = f" + {[c.value for c in classification.secondary_categories]}" if (
        classification.secondary_categories
    ) else ""
    return (
        f"{classification.primary_category.value}{secondary} "
        f"(来源={classification.decided_by}, 估计置信度={classification.confidence:.2f})"
    )
