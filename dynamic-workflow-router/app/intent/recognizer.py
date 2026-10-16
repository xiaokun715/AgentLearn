"""意图识别（说明书 §5）。

这一层的产物是 :class:`~app.domain.models.Intent`，不是「一个意图名」。
理由在 §5.2：只有名字的话，下游要么重新解析一遍 query，要么被迫把业务知识
塞进路由规则里。把「需要哪些能力、大概几步、风险多高」结构化出来之后，
**路由与编排都不再需要理解自然语言**（§2.4）。

分层策略与第十五章的故障分类同源：

    规则命中  ->  直接出结论
    规则不中  ->  LLM 提候选（输出必须过 Schema 校验）
    都不行    ->  UNKNOWN，交给路由去澄清

关于「estimated_steps」（§5.2）：它不是猜的，而是
**覆盖 required_capabilities 最少需要几个 Agent** —— 一个可以算的事实。
"""
from __future__ import annotations

import re

from ..domain.enums import IntentStatus, RiskLevel
from ..domain.models import Intent
from ..domain.policy import INTENT_LIBRARY, IntentSpec

#: 从 query 里粗抽槽位（演示用的最小实现，真机上应该是 NER / 槽位模型）
_NUMBER = re.compile(r"\b(\d+(?:\.\d+)?)\s*(ms|s|分钟|秒|次|%|Mbps)?")
_QUOTED = re.compile(r"[\"'“”]([^\"'“”]{2,40})[\"'“”]")
_IDENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")


class IntentRecognizer:
    """规则优先 + LLM 补充的意图识别器。"""

    def __init__(self, llm=None) -> None:
        self.llm = llm
        self.rule_hits: dict[str, int] = {}

    # ------------------------------------------------------------------
    def recognize(self, query: str, *, forced: str | None = None) -> Intent:
        if forced:
            spec = INTENT_LIBRARY.get(forced)
            if spec is None:
                raise KeyError(f"未知的意图：{forced}")
            return self._build(spec, query, confidence=0.99, decided_by="forced",
                               rationale=f"用户显式指定意图 {forced}")

        scored = self._score_by_rules(query)
        if scored:
            spec, hits, margin = scored[0]
            confidence = min(0.95, 0.45 + 0.15 * hits) * (1.0 if margin else 0.7)
            return self._build(
                spec, query, confidence=round(confidence, 3), decided_by="rule",
                rationale=f"命中关键词 {hits} 个"
                          + ("" if margin else "，但与次候选没有拉开差距"),
            )

        return self._by_llm(query)

    # ------------------------------------------------------------------
    def _score_by_rules(self, query: str) -> list[tuple[IntentSpec, int, bool]]:
        """返回 ``[(spec, 命中数, 是否明显领先), ...]``，按命中数降序。"""
        lowered = query.lower()
        hits: list[tuple[IntentSpec, int]] = []
        for spec in INTENT_LIBRARY.values():
            count = sum(1 for keyword in spec.keywords if keyword.lower() in lowered)
            if count:
                hits.append((spec, count))
        if not hits:
            return []
        hits.sort(key=lambda item: (-item[1], item[0].name))
        self.rule_hits = {spec.name: count for spec, count in hits}
        best_count = hits[0][1]
        runner_up = hits[1][1] if len(hits) > 1 else 0
        # 「明显领先」的判据是**严格大于**次席：并列说明分不开，该让路由去澄清
        clear = best_count > runner_up
        return [(spec, count, clear) for spec, count in hits]

    # ------------------------------------------------------------------
    def _by_llm(self, query: str) -> Intent:
        if self.llm is None:
            return Intent(
                name="unknown", status=IntentStatus.UNKNOWN, confidence=0.0,
                decided_by="unknown", raw_query=query,
                rationale="规则没有命中，也没有可用的 LLM 兜底",
            )
        raw = self.llm.classify_intent(query)
        try:
            intent = Intent.model_validate(raw)
        except Exception:
            return Intent(
                name="unknown", status=IntentStatus.UNKNOWN, confidence=0.0,
                decided_by="unknown", raw_query=query,
                rationale="LLM 输出未通过 Schema 校验",
            )
        intent.decided_by = "llm"
        intent.raw_query = query
        intent.entities = {**self._extract_entities(query), **intent.entities}
        # 模型给的置信度未经校准，统一打折（与第十五章同一条纪律）
        intent.confidence = round(min(intent.confidence, 0.7), 3)
        intent.status = self._status_for(intent.confidence, intent.name)
        return intent

    # ------------------------------------------------------------------
    def _build(self, spec: IntentSpec, query: str, *, confidence: float,
               decided_by: str, rationale: str) -> Intent:
        return Intent(
            name=spec.name,
            status=self._status_for(confidence, spec.name),
            confidence=confidence,
            entities=self._extract_entities(query),
            required_capabilities=list(spec.required_capabilities),
            estimated_steps=spec.estimated_steps,
            risk=spec.risk,
            decided_by=decided_by,
            rationale=rationale,
            raw_query=query,
        )

    @staticmethod
    def _status_for(confidence: float, name: str) -> IntentStatus:
        if name == "unknown":
            return IntentStatus.UNKNOWN
        if confidence < 0.4:
            return IntentStatus.AMBIGUOUS
        return IntentStatus.RESOLVED

    @staticmethod
    def _extract_entities(query: str) -> dict:
        """粗抽槽位。真机上应该走槽位模型，这里只为了演示 entities 会被下游用到。"""
        entities: dict = {}
        quoted = _QUOTED.findall(query)
        if quoted:
            entities["subject"] = quoted[0]
        numbers = _NUMBER.findall(query)
        if numbers:
            entities["numbers"] = [value for value, _unit in numbers]
        idents = [token for token in _IDENT.findall(query)
                  if token.lower() not in {"review", "code", "the", "and", "for"}]
        if idents:
            entities["module"] = idents[0]
        return entities


def explain(intent: Intent) -> str:
    return (
        f"{intent.name}（{intent.status.value}, 置信度 {intent.confidence:.2f}, "
        f"来源={intent.decided_by}, 约 {intent.estimated_steps} 步）"
    )


def detect_ambiguity(intent: Intent) -> list[str]:
    """§5.4 多意图：把候选意图列出来，供澄清时使用。"""
    return list(intent.alternates)


__all__ = ["IntentRecognizer", "explain", "detect_ambiguity", "RiskLevel"]
