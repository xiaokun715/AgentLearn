"""决策引擎（说明书 §1.3）。

把「规则命中了什么」和「模型打了多少分」合成一个处置。

## 规则与模型分歧时，不要合成，要交出去

这是本模块最重要的一条判断。朴素做法是「谁更严听谁的」：

    规则命中 HIGH  → 拦
    模型分高       → 拦

但它掩盖了一个真实且重要的情况：**规则说有问题，模型说没事**。

* 规则匹配的是**已知模式**，它可能因为场景变化而误报（大促期间的高退款率）；
* 模型学的是**历史分布**，它对没见过的新形态天然不敏感。

两者分歧时，正确的动作不是选一个，而是**承认自己不知道** ——
转人工（或者转给 Agent 调查）。这正是本 Demo 里所有告警的来源。

## 在线链路必须是确定性的、毫秒级的

决策引擎里不允许出现任何「看情况」「再查一下」的逻辑。
每一条分支都由已经算好的数决定，总延迟是个位数毫秒（§1.4）。
"""
from __future__ import annotations

from ..domain.enums import Decision, RiskLevel
from ..domain.models import Event, FeatureSet, ModelScore, OnlineDecision, RuleHit
from ..domain.policy import DECISION
from .features import FEATURE_LATENCY_MS
from .risk_model import MODEL_LATENCY_MS
from .rules import RULE_LATENCY_MS

DECISION_LATENCY_MS = 0.1


class DecisionEngine:
    """规则 + 模型 → 处置。"""

    def decide(
        self,
        event: Event,
        features: FeatureSet,
        hits: list[RuleHit],
        model_score: ModelScore,
    ) -> OnlineDecision:
        decision, rationale = self._resolve(hits, model_score, features)
        total_ms = int(FEATURE_LATENCY_MS + RULE_LATENCY_MS + MODEL_LATENCY_MS
                       + DECISION_LATENCY_MS)
        return OnlineDecision(
            event_id=event.event_id,
            decision=decision,
            rule_hits=hits,
            model_score=model_score,
            features=features,
            latency_ms=total_ms,
            rationale=rationale,
        )

    # ------------------------------------------------------------------
    def _resolve(
        self, hits: list[RuleHit], model_score: ModelScore, features: FeatureSet
    ) -> tuple[Decision, str]:
        high_hits = [hit for hit in hits if hit.risk_level is RiskLevel.HIGH]
        medium_hits = [hit for hit in hits if hit.risk_level is RiskLevel.MEDIUM]

        # ---- ① 规则与模型分歧 -> 交出去 ----------------------------------
        if high_hits and model_score.score < DECISION.verify_score:
            return Decision.REVIEW, (
                f"规则 {[h.rule_id for h in high_hits]} 判定高风险，"
                f"但模型分仅 {model_score.score:.3f}（< {DECISION.verify_score}）—— "
                "两者分歧，在线链路不下结论，转人工/调查"
            )

        # ---- ② 双高 -> 拦截 ---------------------------------------------
        if high_hits and model_score.score >= DECISION.verify_score:
            return Decision.BLOCK, (
                f"高风险规则 {[h.rule_id for h in high_hits]} 命中，"
                f"且模型分 {model_score.score:.3f} ≥ {DECISION.verify_score}"
            )

        # ---- ③ 模型单独过线 ---------------------------------------------
        if model_score.score >= DECISION.block_score:
            return Decision.BLOCK, (
                f"模型分 {model_score.score:.3f} ≥ {DECISION.block_score}（拦截线）"
            )
        if model_score.score >= DECISION.verify_score:
            return Decision.VERIFY, (
                f"模型分 {model_score.score:.3f} ≥ {DECISION.verify_score}（验证线）"
            )

        # ---- ④ 规则单独命中 ---------------------------------------------
        if medium_hits:
            return Decision.VERIFY, (
                f"中风险规则 {[h.rule_id for h in medium_hits]} 命中，追加二次验证"
            )
        if len(hits) >= DECISION.review_on_multi_hit:
            return Decision.REVIEW, (
                f"命中 {len(hits)} 条规则但单条都够不上 —— "
                "「合起来可疑」这种情况在线链路没有把握，转人工"
            )

        # ---- ⑤ 模型接近阈值 -> 观察 --------------------------------------
        if model_score.score >= DECISION.review_score:
            return Decision.REVIEW, (
                f"模型分 {model_score.score:.3f} 处于观察区间"
                f"（≥ {DECISION.review_score}），样本入库待后续分析"
            )

        return Decision.PASS, f"无规则命中，模型分 {model_score.score:.3f} 低于全部阈值"

    # ------------------------------------------------------------------
    @staticmethod
    def to_alert(decision: OnlineDecision, event: Event, reason_prefix: str = ""):
        """把一次 ``REVIEW`` 的在线决策升级成告警 —— Agent 链路的入口（§4.1）。"""
        from ..domain.models import Alert

        return Alert(
            rule_ids=decision.hit_rule_ids,
            entity_type="user",
            entity_id=event.user_id,
            reason=reason_prefix + decision.rationale,
            model_score=decision.model_score.score,
            created_at_ms=event.occurred_at_ms,
            context={
                "event_type": event.event_type,
                "device_id": event.device_id,
                "ip": event.ip,
                "campaign_id": event.campaign_id,
                "features": decision.features.values,
            },
        )


__all__ = ["DecisionEngine", "DECISION_LATENCY_MS"]
