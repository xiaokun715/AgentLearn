"""Toxicity Detector（设计说明书 §9 提到的可扩展类别之一）。

MVP：辱骂/攻击性词表（中英）。真正生产可用 Rule+ML Classifier 分级放大。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.context import Stage
from ..core.finding import SecurityFinding
from .base import Detector

if TYPE_CHECKING:
    from ..core.context import GuardrailContext

# 攻击性/辱骂关键词（用于 Demo；词表可按需扩展）
TOXIC_WORDS: tuple[str, ...] = (
    "笨蛋", "白痴", "蠢货", "去死", "贱人", "傻逼", "脑残", "废物",
    "fuck", "shit", "bitch", "asshole", "idiot", "stupid", "dumbass", "loser",
)


class ToxicityDetector(Detector):
    name = "toxicity"
    # 输入与输出都应约束语气
    stages = frozenset({Stage.INPUT, Stage.OUTPUT})

    def __init__(self, words: tuple[str, ...] = TOXIC_WORDS) -> None:
        self.words = words

    async def detect(self, context: "GuardrailContext") -> list[SecurityFinding]:
        text = context.text.lower()
        findings: list[SecurityFinding] = []
        for word in self.words:
            if word.lower() in text:
                findings.append(
                    SecurityFinding(
                        detector=self.name,
                        category="TOXICITY",
                        severity="MEDIUM",
                        confidence=0.9,
                        message=f"toxic language detected: {word}",
                        location=word,
                        metadata={"raw": word},
                    )
                )
        return findings


__all__ = ["ToxicityDetector", "TOXIC_WORDS"]
