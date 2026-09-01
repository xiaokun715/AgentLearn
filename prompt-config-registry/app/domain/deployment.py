"""Deployment 领域模型（设计说明书 §19~§20）。

核心：Deployment = **可变的路由表**（Mutable Routing），与不可变的 Version 分离。

- ``rules`` 是权威路由表：``[{version:12, weight:90}, {version:13, weight:10}]``
- ``previous_rules`` 是发布/灰度前的路由快照，供回滚精确还原（§18）
- ``status`` 沿 §14 的状态机演进：STAGED → CANARY → RELEASED
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeploymentStatus:
    """发布状态机（§14）。DRAFT → VALIDATED → STAGED → CANARY → RELEASED。"""

    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    STAGED = "STAGED"
    CANARY = "CANARY"
    RELEASED = "RELEASED"

    # 允许的推进边（简单状态机，防止乱跳）
    TRANSITIONS: dict[str, set[str]] = {
        DRAFT: {VALIDATED, STAGED, CANARY, RELEASED},
        VALIDATED: {STAGED, CANARY, RELEASED},
        STAGED: {CANARY, RELEASED},
        CANARY: {RELEASED, STAGED},
        RELEASED: set(),  # 已是终态；变更请走新的 publish/rollout
    }

    @classmethod
    def can(cls, current: str, next_: str) -> bool:
        return next_ in cls.TRANSITIONS.get(current, set())

    @classmethod
    def all(cls) -> tuple[str, ...]:
        return (cls.DRAFT, cls.VALIDATED, cls.STAGED, cls.CANARY, cls.RELEASED)


@dataclass(slots=True)
class DeploymentRule:
    """一条路由规则（§20）。weight 为 0~100 的流量百分比。"""

    version: int
    weight: int
    condition: str | None = None  # 进阶：条件路由（如 tenant_id == 'internal'）

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"version": self.version, "weight": self.weight}
        if self.condition:
            data["condition"] = self.condition
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DeploymentRule":
        return cls(
            version=int(data["version"]),
            weight=int(data.get("weight", 0)),
            condition=data.get("condition"),
        )


@dataclass(slots=True)
class Deployment:
    """一个 (agent, environment) 的当前部署状态（相当于 SQL ``deployments`` 的行）。

    注意：**唯一的 (agent, environment) 组合只有一行** —— 发布/灰度/回滚都是
    原地修改这一行的路由表，而不是新开一行。这就是 "Mutable Deployment"。
    """

    id: str
    agent_name: str
    environment: str
    status: str
    rules: list[DeploymentRule]
    experiment: str | None = None
    previous_rules: list[DeploymentRule] | None = None
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def primary_version(self) -> int | None:
        """最高权重的版本 = 当前"老版本 / 对照组"（A 组）。"""
        if not self.rules:
            return None
        return max(self.rules, key=lambda r: r.weight).version

    @property
    def traffic_percent(self) -> int:
        """primary_version 当前的流量占比（用于简单展示）。"""
        if not self.rules:
            return 0
        return max(self.rules, key=lambda r: r.weight).weight

    @property
    def is_single_version(self) -> bool:
        return len(self.rules) == 1

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent_name,
            "environment": self.environment,
            "status": self.status,
            "version": self.primary_version,
            "traffic_percent": self.traffic_percent,
            "rules": [r.to_dict() for r in self.rules],
            "experiment": self.experiment,
            "previous_rules": [r.to_dict() for r in self.previous_rules]
            if self.previous_rules
            else None,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Deployment":
        return cls(
            id=data["id"],
            agent_name=data["agent"],
            environment=data["environment"],
            status=data["status"],
            rules=[DeploymentRule.from_dict(r) for r in data["rules"]],
            experiment=data.get("experiment"),
            previous_rules=[DeploymentRule.from_dict(r) for r in data["previous_rules"]]
            if data.get("previous_rules")
            else None,
            created_by=data.get("created_by", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    def __str__(self) -> str:
        routes = ", ".join(f"v{r.version}:{r.weight}%" for r in self.rules)
        return f"{self.agent_name}/{self.environment} [{self.status}] {{{routes}}}"
