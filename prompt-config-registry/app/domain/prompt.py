"""Prompt 领域模型（设计说明书 §7~§8）。

两个概念：
- ``Prompt``     —— 一个可命名的 Prompt 实体（只存元信息，不存内容）
- ``PromptVersion`` —— 一个**不可变**的具体版本：template + variables + metadata

为什么版本不可变（§38）：LLM 是非确定系统，Prompt 是行为输入。
一次执行必须能精确复现 → v12 一旦发布就永远存在，改动只能产生 v13。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Prompt:
    """Prompt 实体（相当于 SQL ``prompts`` 表的行）。"""

    id: str
    name: str
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Prompt":
        return cls(
            id=data["id"],
            name=data["name"],
            created_by=data.get("created_by", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


@dataclass(slots=True)
class PromptVersion:
    """一个不可变的 Prompt 版本（相当于 SQL ``prompt_versions`` 表的行）。"""

    id: str
    prompt_name: str
    version: int
    template: str
    variables: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self, *, include_template: bool = True) -> dict:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.prompt_name,
            "version": self.version,
            "template": self.template,
            "variables": self.variables,
            "metadata": self.metadata,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
        }
        if not include_template:
            # 列表页可以不返回模板体，节省带宽
            data.pop("template", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PromptVersion":
        return cls(
            id=data["id"],
            prompt_name=data["name"],
            version=data["version"],
            template=data["template"],
            variables=data.get("variables", []),
            metadata=data.get("metadata", {}),
            created_by=data.get("created_by", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    def __str__(self) -> str:  # 便于日志 / 审计展示
        return f"{self.prompt_name}:v{self.version}"
