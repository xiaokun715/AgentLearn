"""Secret Detector（设计说明书 §12）—— API Key / JWT / AWS Key / 私钥 / 口令。

统一产出 category="SECRET"（与 policies.yaml 的 ``SECRET`` 规则对齐），
subtype（OPENAI_KEY / JWT / …）放进 metadata 便于审计与脱敏模板选择。
带捕获组的 pattern 只脱敏「值」部分（如 ``password = 123456`` -> ``<SECRET_REDACTED>``）。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..core.finding import SecurityFinding
from .base import TEXT_STAGES, Detector

if TYPE_CHECKING:
    from ..core.context import GuardrailContext

# subtype -> (pattern, confidence)；组 1 存在时 raw 取组 1（只遮值）
SECRET_RULES: dict[str, tuple[re.Pattern[str], float]] = {
    "OPENAI_KEY": (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), 0.97),
    "ANTHROPIC_KEY": (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"), 0.97),
    "JWT": (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"), 0.95),
    "AWS_ACCESS_KEY": (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0.98),
    "PRIVATE_KEY": (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), 0.99),
    "PASSWORD": (re.compile(r"(?i)(?:password|passwd|pwd)\s*[:=]\s*([^\s,;]+)"), 0.85),
    "TOKEN": (re.compile(r"(?i)(?:access[_-]?token|auth[_-]?token)\s*[:=]\s*([^\s,;]+)"), 0.9),
}


class SecretDetector(Detector):
    name = "secret"
    stages = TEXT_STAGES

    def __init__(self, rules: dict | None = None) -> None:
        self.rules = rules or SECRET_RULES

    async def detect(self, context: "GuardrailContext") -> list[SecurityFinding]:
        text = context.text
        findings: list[SecurityFinding] = []
        for subtype, (pattern, confidence) in self.rules.items():
            for m in pattern.finditer(text):
                # 有捕获组时只把「值」作为 raw（脱敏只替换值，保留字段名）
                raw = m.group(1) if m.lastindex else m.group(0)
                if not raw:
                    continue
                start, end = m.span()
                findings.append(
                    SecurityFinding(
                        detector=self.name,
                        category="SECRET",
                        severity="CRITICAL",
                        confidence=confidence,
                        message=f"{subtype} credential leak detected",
                        location=text[max(0, start - 12): min(len(text), end + 12)],
                        metadata={"raw": raw, "subtype": subtype},
                    )
                )
        return findings


__all__ = ["SecretDetector", "SECRET_RULES"]
