"""规则引擎（说明书 §1.2）。

规则引擎的职责范围**必须被守住**：它只判定「这一笔事件是否匹配一条已知模式」，
不判定「这意味着什么」。

这条边界的实际后果是：规则永远无法发现新模式（它只能匹配已知的），
也永远给不出「为什么」—— 那是 Agent 链路的活（§2.1）。

## 场景相关的阈值（§6.2）

``R003_REFUND_RATIO`` 在促销期间的阈值会上浮。这不是「规则写松了」，
而是**规则必须知道自己在什么场景里被评估**：

大促期间整体退款率天然上升（冲动消费、凑单后退），一条按平时分布标定的
固定阈值在大促期间会大量误报。Demo 的 ``strategy`` 场景就是从这条规则
的误报分析开始的 —— 它也是「规则与 Agent 分工」的一个真实缩影。
"""
from __future__ import annotations

from ..domain.models import Event, FeatureSet, RuleHit
from ..domain.policy import DECISION, ONLINE_RULES, RuleSpec
from ..sim import RiskWorld

RULE_LATENCY_MS = 0.3


class RuleEngine:
    def __init__(self, world: RiskWorld) -> None:
        self.world = world
        self.rules: list[RuleSpec] = list(ONLINE_RULES)
        #: 记录每条规则被放宽过几次 —— 用于策略分析时解释「为什么现在才发现」
        self.relaxed: dict[str, int] = {}

    # ------------------------------------------------------------------
    def effective_threshold(self, rule: RuleSpec, features: FeatureSet) -> float:
        """场景相关的实际阈值。"""
        threshold = rule.threshold
        if rule.rule_id == "R003_REFUND_RATIO" and features.get("is_promo_scene"):
            threshold *= (1.0 + DECISION.promo_refund_relax)
            self.relaxed[rule.rule_id] = self.relaxed.get(rule.rule_id, 0) + 1
        return threshold

    # ------------------------------------------------------------------
    def evaluate(self, event: Event, features: FeatureSet) -> list[RuleHit]:
        hits: list[RuleHit] = []
        for rule in self.rules:
            value = features.get(rule.feature)
            threshold = self.effective_threshold(rule, features)
            triggered = (value > threshold if rule.direction == "above"
                         else value < threshold)
            if not triggered:
                continue
            # R003 需要「笔数达到门槛」才成立 —— 单看比率会被小样本骗
            if rule.rule_id == "R003_REFUND_RATIO" and features.get("refund_count_7d") < 5:
                continue
            hits.append(RuleHit(
                rule_id=rule.rule_id,
                description=rule.description,
                risk_level=rule.risk_level,
                evidence={
                    rule.feature: value,
                    "threshold": round(threshold, 4),
                    "promo_relaxed": threshold != rule.threshold,
                },
            ))
        return hits

    # ------------------------------------------------------------------
    def rule_spec(self, rule_id: str) -> RuleSpec | None:
        return next((rule for rule in self.rules if rule.rule_id == rule_id), None)
