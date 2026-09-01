"""审计日志领域模型（设计说明书 §24~§25）。

每次创建 / 修改 / 发布 / 灰度 / 回滚都要记录 before / after，
这样线上行为突变时可以做到 **Change Attribution**：

    22:01  Prompt v12
    22:10  Prompt v13 deployed   ← 看审计日志就知道"到底变了什么"
    22:15  Tool error ↑
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditAction:
    CREATE_PROMPT = "CREATE_PROMPT"
    CREATE_PROMPT_VERSION = "CREATE_PROMPT_VERSION"
    CREATE_CONFIG = "CREATE_CONFIG"
    DEPLOY = "DEPLOY"
    ROLLOUT = "ROLLOUT"
    ROLLBACK = "ROLLBACK"
    DELETE = "DELETE"


@dataclass(slots=True)
class AuditEntry:
    action: str
    resource_type: str
    resource_id: str
    actor: str = ""
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str = ""
    id: int | None = None            # BIGSERIAL / AUTOINCREMENT
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "actor": self.actor,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuditEntry":
        return cls(
            action=data["action"],
            resource_type=data["resource_type"],
            resource_id=data["resource_id"],
            actor=data.get("actor", ""),
            before=data.get("before"),
            after=data.get("after"),
            reason=data.get("reason", ""),
            id=data.get("id"),
            created_at=datetime.fromisoformat(data["created_at"]),
        )
