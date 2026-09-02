"""Human Approval 测试（设计说明书 §28、Demo 7）。"""
from __future__ import annotations

import pytest

from app.approval.service import ApprovalService
from app.core.exceptions import InvalidApprovalError, NotFoundError


def _req(service: ApprovalService, **kw) -> object:
    return service.create(
        request_id="req_1", tenant_id="t", agent="environment_recovery",
        tool="execute_shell", arguments={"command": "ls"}, risk_level="HIGH", **kw,
    )


async def test_approval_lifecycle(g):
    check = await g.check_tool(
        "environment_recovery", "execute_shell", {"command": "ls"}
    )
    approval_id = check.approval_id
    assert approval_id

    pending = g.list_approvals("PENDING")
    assert any(a["id"] == approval_id for a in pending)

    decided = g.decide_approval(approval_id, approved=True, decided_by="ops", note="ok")
    assert decided["status"] == "APPROVED"
    assert decided["decided_by"] == "ops"


async def test_decide_twice_conflict(g):
    check = await g.check_tool(
        "environment_recovery", "delete_file", {"path": "/tmp/a.log"}
    )
    g.decide_approval(check.approval_id, approved=True)
    with pytest.raises(InvalidApprovalError):
        g.decide_approval(check.approval_id, approved=False)


async def test_unknown_approval_not_found(g):
    with pytest.raises(NotFoundError):
        g.get_approval("apr_nonexistent")


def test_expired_approval_transitions():
    service = ApprovalService(ttl_seconds=-1)
    req = _req(service)
    req = service.get(req.id)  # 惰性过期
    assert req.status == "EXPIRED"
    with pytest.raises(InvalidApprovalError):
        service.decide(req.id, approved=True)
