"""Audit Service —— 每一次变更都留下 before/after（设计说明书 §24~§25）。

为什么审计重要（§25）：线上 Agent 行为突变时，能回答
「到底是什么变了」—— Change Attribution：

    22:01  Prompt v12 正常
    22:10  Prompt v13 deployed      ← 审计日志定位到这一条
    22:15  Tool error ↑
"""
from __future__ import annotations

from typing import Any

from ..domain.audit import AuditEntry
from ..storage.repository import RegistryRepository


class AuditService:
    def __init__(self, repo: RegistryRepository) -> None:
        self._repo = repo

    async def record(
        self,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason: str = "",
    ) -> AuditEntry:
        """记录一条审计日志。before/after 存精简的 dict（避免把超大模板也塞进去）。"""
        entry = AuditEntry(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before=before,
            after=after,
            reason=reason,
        )
        return await self._repo.append_audit(entry)

    async def list(
        self, *, limit: int = 100, action: str | None = None, agent: str | None = None
    ) -> list[AuditEntry]:
        return await self._repo.list_audit(limit=limit, action=action, agent=agent)
