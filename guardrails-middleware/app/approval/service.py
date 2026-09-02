"""Human Approval 服务（设计说明书 §28）。

高风险 Tool（execute_shell / delete_file / production_deploy ...）命中后创建一张
PENDING 票据，等待人类 Approve / Reject；超过 TTL 自动 EXPIRED。

线程安全：get/list/decide 的「查状态 + 改状态」在锁内原子完成（check-and-set），
避免并发 decide 双批放行（review 修复）。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..core.exceptions import InvalidApprovalError, NotFoundError

APPROVAL_STATUSES = ("PENDING", "APPROVED", "REJECTED", "EXPIRED")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ApprovalRequest:
    id: str
    request_id: str
    tenant_id: str
    agent: str
    tool: str
    arguments: dict
    risk_level: str
    status: str = "PENDING"
    reason: str = ""
    created_at: datetime = field(default_factory=_now)
    expires_at: datetime = field(default_factory=lambda: _now() + timedelta(seconds=900))
    decided_at: datetime | None = None
    decided_by: str = ""
    decision_note: str = ""

    @property
    def expired(self) -> bool:
        return _now() > self.expires_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "agent": self.agent,
            "tool": self.tool,
            "arguments": self.arguments,
            "risk_level": self.risk_level,
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
            "decision_note": self.decision_note,
        }


class ApprovalService:
    """内存态票据存储（Demo 用）；生产可换 DB + 轮询/回调。"""

    def __init__(self, ttl_seconds: int = 900) -> None:
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        request_id: str,
        tenant_id: str,
        agent: str,
        tool: str,
        arguments: dict,
        risk_level: str,
        reason: str = "",
    ) -> ApprovalRequest:
        now = _now()
        req = ApprovalRequest(
            id=f"apr_{uuid.uuid4().hex[:16]}",
            request_id=request_id,
            tenant_id=tenant_id,
            agent=agent,
            tool=tool,
            arguments=arguments or {},
            risk_level=risk_level,
            reason=reason,
            created_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
        )
        with self._lock:
            self._store[req.id] = req
        return req

    def _expire_locked(self, req: ApprovalRequest) -> None:
        # 惰性过期：查询/审批时才把超时 PENDING 标记为 EXPIRED（须在锁内调用）
        if req.status == "PENDING" and req.expired:
            req.status = "EXPIRED"

    def get(self, approval_id: str) -> ApprovalRequest:
        with self._lock:
            req = self._store.get(approval_id)
            if req is None:
                raise NotFoundError(f"approval request not found: {approval_id}")
            self._expire_locked(req)
            return req

    def list(self, status: str | None = None) -> list[ApprovalRequest]:
        with self._lock:
            items = list(self._store.values())
            for req in items:
                self._expire_locked(req)
            if status:
                items = [r for r in items if r.status == status.upper()]
            items = sorted(items, key=lambda r: r.created_at, reverse=True)
        return items

    def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str = "human",
        note: str = "",
    ) -> ApprovalRequest:
        # 整段 check-and-set 放在锁内：并发 decide 只有一个能通过
        with self._lock:
            req = self._store.get(approval_id)
            if req is None:
                raise NotFoundError(f"approval request not found: {approval_id}")
            self._expire_locked(req)
            if req.status != "PENDING":
                raise InvalidApprovalError(
                    f"approval request {approval_id} is {req.status}, cannot decide again"
                )
            req.status = "APPROVED" if approved else "REJECTED"
            req.decided_at = _now()
            req.decided_by = decided_by
            req.decision_note = note
            return req


__all__ = ["ApprovalService", "ApprovalRequest", "APPROVAL_STATUSES"]
