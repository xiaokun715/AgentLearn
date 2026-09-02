"""SecurityEvent（设计说明书 §29）。

记录「谁 / 什么时候 / 哪个 Agent / 哪个 Tool / 哪个 Detector / 发现了什么 /
采取了什么动作」，供事后追溯与 Metrics 统计。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SecurityEvent:
    request_id: str
    tenant_id: str
    user_id: str
    agent: str
    stage: str
    detector: str
    category: str
    severity: str
    action: str
    timestamp: datetime = field(default_factory=_utcnow)
    metadata: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")

    def to_dict(self) -> dict:
        d = {
            "event_id": self.event_id,
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "agent": self.agent,
            "stage": self.stage,
            "detector": self.detector,
            "category": self.category,
            "severity": self.severity,
            "action": self.action,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.metadata:
            d["metadata"] = self.metadata
        return d


__all__ = ["SecurityEvent"]
