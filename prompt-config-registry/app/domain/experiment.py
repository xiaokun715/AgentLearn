"""Experiment 领域模型（设计说明书 §15~§17 / §31）。

A/B 实验的元数据：一个实验把某个版本的流量切给多个 Variant，
Resolver 在 routing 里带上 experiment + variant，供后续做
「Variant A Success=87% vs Variant B Success=91%」归因（§35）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ExperimentVariant:
    """实验中的一个变体（版本 + 权重）。"""

    name: str                       # "A" / "B" ...
    version: int
    weight: int

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "weight": self.weight}

    @classmethod
    def from_dict(cls, data: dict) -> "ExperimentVariant":
        return cls(
            name=data["name"],
            version=int(data["version"]),
            weight=int(data["weight"]),
        )


@dataclass(slots=True)
class Experiment:
    """A/B 实验元数据（可作为 Deployment.experiment 引用的结构化形式）。"""

    name: str
    agent: str
    environment: str
    variants: list[ExperimentVariant] = field(default_factory=list)
    status: str = "RUNNING"          # RUNNING | PROMOTED | ROLLED_BACK
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "agent": self.agent,
            "environment": self.environment,
            "variants": [v.to_dict() for v in self.variants],
            "status": self.status,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Experiment":
        return cls(
            name=data["name"],
            agent=data["agent"],
            environment=data["environment"],
            variants=[ExperimentVariant.from_dict(v) for v in data.get("variants", [])],
            status=data.get("status", "RUNNING"),
            created_by=data.get("created_by", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
        )
