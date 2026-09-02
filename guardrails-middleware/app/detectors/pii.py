"""PII Detector（设计说明书 §10）—— 手机号 / 邮箱 / 身份证 / 银行卡。

确定性优先：全部用正则，不用 LLM（§39 工程原则：能确定判断的不交给模型）。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..core.finding import SecurityFinding
from .base import TEXT_STAGES, Detector, find_all

if TYPE_CHECKING:
    from ..core.context import GuardrailContext

# category -> (pattern, severity, confidence)
PII_RULES: dict[str, tuple[re.Pattern[str], str, float]] = {
    # 中国大陆手机号 1[3-9]xxxxxxxxx
    "PHONE": (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "MEDIUM", 0.99),
    # 邮箱
    "EMAIL": (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "MEDIUM", 0.99),
    # 18 位身份证号（宽松结构校验）
    "ID_CARD": (
        re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
        "HIGH",
        0.9,
    ),
    # 银行卡号 16~19 位
    "BANK_CARD": (re.compile(r"(?<!\d)(?:62|4|5[1-5])[\d]{14,17}(?!\d)"), "HIGH", 0.92),
}


class PIIDetector(Detector):
    name = "pii"
    # PII 出现在一切「自由文本」边界（输入 / 外部上下文 / Tool 结果 / 输出）
    stages = TEXT_STAGES

    def __init__(self, rules: dict | None = None) -> None:
        self.rules = rules or PII_RULES

    async def detect(self, context: "GuardrailContext") -> list[SecurityFinding]:
        text = context.text
        findings: list[SecurityFinding] = []
        for category, (pattern, severity, confidence) in self.rules.items():
            for raw, start, end in find_all(pattern, text):
                findings.append(
                    SecurityFinding(
                        detector=self.name,
                        category=category,
                        severity=severity,
                        confidence=confidence,
                        message=f"{category} detected",
                        location=text[max(0, start - 12): min(len(text), end + 12)],
                        metadata={"raw": raw},
                    )
                )
        return findings


__all__ = ["PIIDetector", "PII_RULES"]
