"""SecurityFinding —— Detector 的输出（设计说明书 §7）。

Detector 不直接回答「BLOCK / ALLOW」，只回答「发现了什么风险」，
由 Policy Engine 决定如何处理。``location`` 记录命中原文位置/片段，
``metadata["raw"]`` 保存命中原文，供 Redactor / Sanitizer 精确改写。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# severity 排序（越高越危险）
SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


@dataclass
class SecurityFinding:
    detector: str
    category: str
    severity: str
    confidence: float
    message: str
    location: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_RANK:
            raise ValueError(f"unknown severity: {self.severity}")

    @property
    def raw(self) -> str | None:
        """命中原文（Redactor / Sanitizer 用它做精确替换，避免正则二次误伤）。"""
        return self.metadata.get("raw")

    def to_dict(self, minimal: bool = False) -> dict:
        if minimal:
            return {"category": self.category, "severity": self.severity}
        d: dict[str, Any] = {
            "detector": self.detector,
            "category": self.category,
            "severity": self.severity,
            "confidence": round(self.confidence, 3),
            "message": self.message,
        }
        if self.location is not None:
            d["location"] = self.location
        if self.metadata:
            meta = dict(self.metadata)
            if "raw" in meta:
                meta["raw"] = "<hidden>"
            d["metadata"] = meta
        return d


__all__ = ["SecurityFinding", "SEVERITY_RANK"]
