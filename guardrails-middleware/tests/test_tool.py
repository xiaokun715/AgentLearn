"""Tool Guardrail 测试（设计说明书 §18~§20、Demo 4 / 5 / 7）。"""
from __future__ import annotations

from app.core.decision import Action


async def test_unknown_tool_blocked(g):
    r = await g.check_tool("default", "drop_database", {})
    assert r.blocked is True
    assert "allowlist" in r.reason
    assert r.risk_level == "UNKNOWN"


async def test_agent_not_allowed_blocked(g):
    # Demo 4：Agent A 调用 delete_file 但不在 allowed_agents -> BLOCK
    r = await g.check_tool("fault_diagnosis", "delete_file", {"path": "/tmp/x.log"})
    assert r.blocked is True
    assert r.action == Action.BLOCK
    assert "not allowed" in r.reason


async def test_dangerous_argument_blocked(g):
    # Demo 5：路径越界 /etc/passwd -> BLOCK（资源边界）
    r = await g.check_tool(
        "environment_recovery", "delete_file", {"path": "/etc/passwd"}
    )
    assert r.blocked is True
    assert r.risk_level == "CRITICAL"
    assert "resource boundary" in r.reason


async def test_high_risk_tool_requires_approval(g):
    # Demo 7：execute_shell（HIGH + require_approval）-> HUMAN_APPROVAL 票据
    r = await g.check_tool(
        "environment_recovery", "execute_shell", {"command": "ls /tmp"}
    )
    assert r.action == Action.HUMAN_APPROVAL
    assert r.approval_id is not None
    assert not r.blocked


async def test_low_risk_allowed(g):
    r = await g.check_tool(
        "default", "web_search", {"query": "5G 基站告警手册"}
    )
    assert r.action == Action.ALLOW
    assert r.blocked is False


async def test_argument_missing_required_blocked(g):
    r = await g.check_tool("default", "search_document", {})
    assert r.blocked is True
    assert "required" in r.reason
