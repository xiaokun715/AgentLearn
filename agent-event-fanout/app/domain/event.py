"""Event 领域模型（设计说明书 §04~§05）。

> Event 表示「发生了什么」。Event 一旦创建即 **Immutable**，不可修改。
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def new_id(prefix: str) -> str:
    """生成形如 ``evt_xxx`` / ``sub_xxx`` / ``del_xxx`` 的短 ID。"""
    return f"{prefix}_{secrets.token_hex(6)}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def unix_ts(dt: datetime | None = None) -> int:
    """转 Unix 秒（签名时间戳用，设计 §14）。"""
    return int((dt or utcnow()).timestamp())


# ---- 预定义 Event Type（设计 §05） -----------------------------------------
EVENT_TYPES: dict[str, str] = {
    "agent.job.created": "Job 已创建",
    "agent.job.running": "Job 开始运行",
    "agent.job.completed": "Job 完成",
    "agent.job.failed": "Job 失败",
    "agent.tool.started": "Tool 调用开始",
    "agent.tool.completed": "Tool 调用完成",
    "agent.tool.failed": "Tool 调用失败",
    "agent.workflow.completed": "Workflow 完成",
    "agent.workflow.failed": "Workflow 失败",
}


def validate_event_type(event_type: str) -> None:
    """Event Type 必须形如 ``{domain}.{resource}.{action}``（至少两段）。"""
    from .exceptions import EventTypeError

    parts = event_type.split(".")
    if len(parts) < 2 or not all(p.isidentifier() for p in parts):
        raise EventTypeError(
            f"Event Type '{event_type}' 不合法，应为 '{{domain}}.{{resource}}.{{action}}'"
        )


@dataclass(slots=True)
class Event:
    """不可变事件。字段对齐设计 §04。"""

    id: str
    type: str
    tenant_id: str
    created_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    # 可选元数据，例如 §43 的 config_version / prompt_version / model
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        type_: str,
        data: dict[str, Any],
        *,
        tenant_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> "Event":
        validate_event_type(type_)
        return cls(
            id=new_id("evt"),
            type=type_,
            tenant_id=tenant_id,
            created_at=utcnow(),
            data=data,
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at.isoformat(),
            "data": self.data,
            "metadata": self.metadata,
        }
