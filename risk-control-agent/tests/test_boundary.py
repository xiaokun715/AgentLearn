"""工具网关与权限边界的测试（说明书 §3.2）。

**这是本 Demo 最重要的一组测试。** 「Agent 的自主性必须受到约束」这条主张，
只有在代码层面被断言才算数 —— 一条靠提示词维持的约束等于没有约束。
"""
from __future__ import annotations

import pytest

from app.domain.enums import RiskLevel, ToolDenyReason, ToolKind
from app.domain.models import ToolCall
from app.domain.policy import AGENT_GRANTS, APPROVAL_GRANTS, TOOL_INDEX
from app.gateway.tool_gateway import build_scope


# ======================================================================
# 权限集本身
# ======================================================================
def test_agent_grants_exclude_enforcement_and_admin():
    """最核心的一条断言。"""
    assert "risk:enforce" not in AGENT_GRANTS
    assert "risk:admin" not in AGENT_GRANTS


def test_admin_tools_have_no_approval_path():
    """改风控配置没有「Agent 申请一下就放行」的通道。"""
    for name in APPROVAL_GRANTS:
        assert TOOL_INDEX[name].kind is not ToolKind.ADMIN, name


def test_every_side_effect_tool_has_permission_and_risk(platform):
    for spec in platform.tools.values():
        if spec.side_effect:
            assert spec.permission, spec.name
            assert spec.risk is not RiskLevel.LOW, spec.name


# ======================================================================
# 越权调用
# ======================================================================
def test_enforcement_calls_are_denied(platform):
    scope = build_scope(platform.world, "u_fraud_00")
    for tool, args in [
        ("apply_disposition", {"entity_id": "u_fraud_00", "action": "KEEP_BLOCK"}),
        ("release_block", {"entity_id": "u_fraud_00"}),
    ]:
        verdict = platform.gateway.check(ToolCall(tool=tool, arguments=args), scope=scope)
        assert not verdict.allowed
        assert verdict.deny_reason is ToolDenyReason.NO_PERMISSION


def test_admin_calls_are_denied(platform):
    for tool, args in [
        ("update_rule_config", {"rule_id": "R003_REFUND_RATIO", "threshold": 0.9}),
        ("publish_strategy", {"candidate_id": "sc_1"}),
    ]:
        verdict = platform.gateway.check(ToolCall(tool=tool, arguments=args))
        assert not verdict.allowed
        assert verdict.deny_reason is ToolDenyReason.NO_PERMISSION


def test_unknown_tool_is_denied(platform):
    verdict = platform.gateway.check(ToolCall(tool="drop_database", arguments={}))
    assert verdict.deny_reason is ToolDenyReason.UNKNOWN_TOOL


def test_read_tools_are_allowed(platform):
    scope = build_scope(platform.world, "u_fraud_00")
    verdict = platform.gateway.check(
        ToolCall(tool="query_account_profile", arguments={"user_id": "u_fraud_00"}),
        scope=scope)
    assert verdict.allowed


# ======================================================================
# 实体范围
# ======================================================================
def test_query_outside_scope_is_denied(platform):
    scope = build_scope(platform.world, "u_fraud_00")
    outsider = next(uid for uid in platform.world.users if uid not in scope)
    verdict = platform.gateway.check(
        ToolCall(tool="query_account_profile", arguments={"user_id": outsider}), scope=scope)
    assert verdict.deny_reason is ToolDenyReason.OUT_OF_SCOPE


def test_scope_contains_direct_relations_only(platform):
    """授权的是「实体 + 直接关联」，不是全库。"""
    scope = build_scope(platform.world, "u_fraud_00")
    assert "u_fraud_00" in scope
    assert any(item.startswith("dev_") for item in scope)
    assert len(scope) < len(platform.world.users)


# ======================================================================
# 参数校验
# ======================================================================
def test_missing_required_args_are_denied(platform):
    verdict = platform.gateway.check(ToolCall(tool="query_device_graph", arguments={}))
    assert verdict.deny_reason is ToolDenyReason.BAD_ARGUMENTS


def test_wrong_tool_for_wrong_kind_of_argument(platform):
    """参数齐全但语义不对，应该在工具层被处理，而不是网关 ——
    网关只做「形状」校验，业务语义校验属于工具自己。"""
    verdict = platform.gateway.check(
        ToolCall(tool="query_account_profile", arguments={"user_id": "ghost_user"}),
        scope={"ghost_user"})
    assert verdict.allowed      # 形状合法
    from app.tools import investigation

    outcome = investigation.query_account_profile(platform.world, {"user_id": "ghost_user"})
    assert outcome.observation.value == "INCONCLUSIVE"


# ======================================================================
# 审批：一次性动作授权
# ======================================================================
def test_approval_grants_a_single_action(platform):
    gateway = platform.gateway
    approval = gateway.grant_approval("apply_disposition", "AP_1", approver="reviewer-01")
    verdict = gateway.check(
        ToolCall(tool="apply_disposition",
                 arguments={"entity_id": "u_fraud_00", "action": "KEEP_BLOCK"},
                 approval_id=approval),
        scope={"u_fraud_00"})
    assert verdict.allowed


def test_approval_is_not_a_permanent_permission(platform):
    """审批的是「这一次调用」，不是「以后这类事你都可以自己做」。"""
    gateway = platform.gateway
    approval = gateway.grant_approval("apply_disposition", "AP_1", approver="reviewer-01")
    other = gateway.check(
        ToolCall(tool="release_block", arguments={"entity_id": "u_fraud_00"},
                 approval_id=approval),
        scope={"u_fraud_00"})
    assert not other.allowed


def test_admin_action_cannot_be_approved(platform):
    with pytest.raises(PermissionError):
        platform.gateway.grant_approval("update_rule_config", "AP_2", approver="x")


def test_enforcement_without_approval_id_is_denied(platform):
    gateway = platform.gateway
    gateway.grant_approval("apply_disposition", "AP_1", approver="r")
    # 有审批号但没带上，同样拒绝
    verdict = gateway.check(
        ToolCall(tool="apply_disposition",
                 arguments={"entity_id": "u_fraud_00", "action": "KEEP_BLOCK"}),
        scope={"u_fraud_00"})
    assert not verdict.allowed


# ======================================================================
# 预算
# ======================================================================
def test_cost_budget_blocks_calls(platform):
    verdict = platform.gateway.check(
        ToolCall(tool="run_graph_analysis", arguments={"device_id": "dev_pool_a"}),
        used_budget=11.5, cost_limit=12.0)
    assert verdict.deny_reason is ToolDenyReason.BUDGET_EXHAUSTED


# ======================================================================
# 审计
# ======================================================================
def test_every_call_is_audited(platform):
    gateway = platform.gateway
    gateway.check(ToolCall(tool="query_account_profile", arguments={"user_id": "u"}), scope={"u"})
    gateway.check(ToolCall(tool="apply_disposition", arguments={}), scope={"u"})
    snapshot = gateway.snapshot()
    assert len(gateway.audit) == snapshot["total"] == 2


def test_denials_are_counted_by_reason(platform):
    gateway = platform.gateway
    gateway.check(ToolCall(tool="apply_disposition", arguments={}))
    gateway.check(ToolCall(tool="nope", arguments={}))
    reasons = gateway.snapshot()["denied_by_reason"]
    assert reasons.get("NO_PERMISSION") == 1
    assert reasons.get("UNKNOWN_TOOL") == 1


def test_dangerous_attempts_are_flagged(platform):
    platform.gateway.check(ToolCall(tool="update_rule_config", arguments={}))
    assert "update_rule_config" in platform.gateway.snapshot()["dangerous_attempts"]


def test_audit_records_allowed_and_denied(platform):
    gateway = platform.gateway
    gateway.check(ToolCall(tool="query_account_profile", arguments={"user_id": "u"},
                           reason="调查"), scope={"u"})
    gateway.check(ToolCall(tool="apply_disposition", arguments={}), scope={"u"})
    tail = gateway.audit_tail(2)
    assert tail[0].allowed and not tail[1].allowed
    assert tail[1].deny_reason == "NO_PERMISSION"


def test_denied_tool_is_not_retried_in_investigation(make_platform):
    """被拦下的工具要记一笔就继续，不能反复重试 —— 那只会刷屏审计日志。"""
    platform = make_platform("group_fraud")
    report, state = platform.investigate(platform.make_alert("u_fraud_00", "x"))
    assert len(state.denied) == len(set(state.denied))
