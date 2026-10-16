"""风险模型（说明书 §1.2）。

本 Demo 用一个人为设定权重的线性模型替代 GBDT —— 因为要考察的是
**模型的输出在两条链路里怎么被使用**，不是模型本身的效果。

有一点值得强调：**模型只给分数，不给理由**（`top_features` 只是近似的
归因）。这是它的固有局限，也是「为什么还需要 Agent」的一部分：

    规则能回答「哪条规则命中了」
    模型能回答「风险有多高」
    但都回答不了「这件事到底发生了什么、还需要查什么」

后两个问题正是 Agent 链路的输入（§2.1）。
"""
from __future__ import annotations

import math

from ..domain.models import Event, FeatureSet, ModelScore

MODEL_LATENCY_MS = 1.5

#: 特征 -> (权重, 归一化上界)。权重是人为设定的，**负数表示「像正常用户」**。
_FEATURE_WEIGHTS: list[tuple[str, float, float, str]] = [
    ("accounts_per_device_1d", 0.60, 5.0, "同设备账号数"),
    ("refund_ratio_7d", 0.50, 1.0, "退款率"),
    ("coupon_claims_1d", 0.35, 10.0, "单日领券次数"),
    ("is_farm_device", 0.30, 1.0, "模拟器/群控设备"),
    ("accounts_per_ip_1h", 0.25, 20.0, "同 IP 账号数"),
    ("refund_count_7d", 0.15, 20.0, "退款笔数"),
    ("account_level", -0.20, 5.0, "账号等级"),
    ("order_count", -0.10, 200.0, "历史订单数"),
]

_BIAS = -1.20


class RiskModel:
    """确定性的线性模型 + sigmoid。"""

    def __init__(self, version: str = "risk-linear-v1") -> None:
        self.version = version

    # ------------------------------------------------------------------
    def score(self, event: Event, features: FeatureSet) -> ModelScore:
        total = _BIAS
        contributions: list[tuple[str, float]] = []
        for key, weight, ceiling, label in _FEATURE_WEIGHTS:
            raw = features.get(key)
            normalized = min(1.0, abs(raw) / ceiling) if ceiling else 0.0
            contribution = weight * normalized
            total += contribution
            contributions.append((label, round(contribution, 4)))

        probability = 1.0 / (1.0 + math.exp(-total))
        contributions.sort(key=lambda item: -abs(item[1]))
        return ModelScore(
            score=round(probability, 4),
            model_version=self.version,
            top_features=contributions[:4],
        )

    # ------------------------------------------------------------------
    def explain(self, score: ModelScore) -> str:
        parts = [f"{label}{value:+.3f}" for label, value in score.top_features]
        return f"模型 {score.model_version} 打分 {score.score:.3f}；主要贡献：" + "、".join(parts)


__all__ = ["RiskModel", "MODEL_LATENCY_MS", "_FEATURE_WEIGHTS"]
