"""受控恢复测试（说明书 §7）。

这一层的测试重心不是「动作执行成功」，而是**不该执行的时候是否真的没执行**：
没有证据不动、没审批不动、R4 永远不动、阶梯用尽不再试。
"""
from __future__ import annotations

import pytest

from app.domain.enums import RiskTier, TerminalState
from app.hypothesis.manager import HypothesisManager
from app.recovery.controller import RECOVERY_LADDER, RecoveryController


def _supported(platform, key: str = "H_ENV", evidence: int = 2) -> HypothesisManager:
    """构造一个「有客观证据」的假设集合，用来穿过验证门第一关。"""
    manager = HypothesisManager.from_keys({key: 1.0})
    manager.get(key).supporting_evidence = [f"EV_{i}" for i in range(evidence)]
    return manager


# ======================================================================
# 验证门
# ======================================================================
def test_gate_blocks_without_objective_evidence(env_platform):
    manager = _supported(env_platform, evidence=0)
    manager.get("H_ENV").supporting_evidence = []
    decision = env_platform.recovery.evaluate(manager)
    assert decision.action is None
    assert decision.blocked_reason == "NO_EVIDENCE"
    assert decision.tier is RiskTier.R0


def test_gate_blocks_when_no_automated_recovery_exists(env_platform):
    decision = env_platform.recovery.evaluate(_supported(env_platform, "H_CONFIG"))
    assert decision.action is None
    assert decision.blocked_reason == "NO_AUTOMATED_RECOVERY"


def test_gate_blocks_r4_forever(env_platform):
    """R4 没有阶梯，所以走不到；即便走到，也必须被 forbidden 挡住。"""
    manager = _supported(env_platform, "H_CASE")
    decision = env_platform.recovery.evaluate(manager)
    assert decision.action is None
    assert decision.blocked_reason == "NO_AUTOMATED_RECOVERY"
    # 直接构造一个 R4 动作，确认分级本身也是 forbidden
    spec = env_platform.pool.require("override_test_expectation")
    assert spec.tier.forbidden
    assert not spec.tier.needs_approval
    assert not spec.tier.auto_executable


def test_gate_requires_approval_for_r3(env_platform):
    decision = env_platform.recovery.evaluate(
        _supported(env_platform), attempted=["refresh_environment_state",
                                             "reallocate_environment"]
    )
    assert decision.action == "restart_device"
    assert decision.requires_approval
    assert not decision.approved
    assert decision.blocked_reason == "NEEDS_APPROVAL"


def test_gate_passes_after_approval(env_platform):
    env_platform.recovery.grant("restart_device", reviewer="ops-lead")
    decision = env_platform.recovery.evaluate(
        _supported(env_platform),
        attempted=["refresh_environment_state", "reallocate_environment"],
    )
    assert decision.approved
    assert decision.blocked_reason is None


def test_grant_refuses_r4(env_platform):
    with pytest.raises(PermissionError):
        env_platform.recovery.grant("override_test_expectation")


def test_gate_respects_recovery_budget(env_platform):
    env_platform.config.budget.max_recovery_attempts = 1
    controller = RecoveryController(
        env_platform.pool,
        env_platform.registry,
        env_platform.store,
        env_platform.clock,
        permissions=set(env_platform.config.permissions),
        budget=env_platform.config.budget,
    )
    decision = controller.evaluate(
        _supported(env_platform), usage=_usage(recovery_attempts=1)
    )
    assert decision.blocked_reason == "RECOVERY_BUDGET_EXHAUSTED"
    assert not decision.approved
    # 动作字段保留下来：被预算拦住时，运维需要知道「本来要做什么」
    assert decision.action == "refresh_environment_state"


def test_gate_reports_ladder_exhausted(env_platform):
    decision = env_platform.recovery.evaluate(
        _supported(env_platform), attempted=list(RECOVERY_LADDER["H_ENV"])
    )
    assert decision.action is None
    assert decision.blocked_reason == "LADDER_EXHAUSTED"


def _usage(**kwargs):
    from app.domain.models import BudgetUsage

    return BudgetUsage(**kwargs)


# ======================================================================
# 执行与验证
# ======================================================================
def test_r1_leaves_world_untouched(env_platform):
    """R1 的定位就是「无副作用的状态刷新」—— 快照必须完全一致。"""
    manager = _supported(env_platform)
    decision = env_platform.recovery.evaluate(manager)
    assert decision.action == "refresh_environment_state"
    usage = _usage()
    outcome = env_platform.recovery.execute(decision, env_platform.tool_ctx, usage)
    assert outcome.executed
    assert outcome.before_snapshot == outcome.after_snapshot
    assert usage.recovery_attempts == 1
    assert usage.tool_calls == 1


def test_r2_changes_world_and_records_snapshots(env_platform):
    """R2 会真的改世界，所以前后快照必须不同（§7.2 要求留档）。"""
    manager = _supported(env_platform)
    decision = env_platform.recovery.evaluate(
        manager, attempted=["refresh_environment_state"]
    )
    assert decision.action == "reallocate_environment"
    outcome = env_platform.recovery.execute(decision, env_platform.tool_ctx, _usage())
    assert outcome.executed
    assert outcome.before_snapshot != outcome.after_snapshot
    assert outcome.before_snapshot["env_ready"] is False
    assert outcome.after_snapshot["env_ready"] is True


def test_ineffective_action_is_not_verified(env_platform):
    """refresh 只重查状态，不会修好未初始化的环境。"""
    decision = env_platform.recovery.evaluate(_supported(env_platform))
    outcome = env_platform.recovery.execute(
        decision, env_platform.tool_ctx, _usage()
    )
    verified = env_platform.recovery.verify(
        outcome, env_platform.tool_ctx, _usage(), root_cause_evidence=["EV_1"]
    )
    assert verified.verified is False
    assert verified.rerun_passed is None


def test_effective_action_runs_rerun_and_records_verification(env_platform):
    manager = _supported(env_platform)
    decision = env_platform.recovery.evaluate(
        manager, attempted=["refresh_environment_state"]
    )
    assert decision.action == "reallocate_environment"
    usage = _usage()
    outcome = env_platform.recovery.execute(decision, env_platform.tool_ctx, usage)
    outcome = env_platform.recovery.verify(
        outcome, env_platform.tool_ctx, usage,
        root_cause_evidence=manager.leader().supporting_evidence,
    )
    assert outcome.verified is True
    assert outcome.rerun_passed is True
    # 一条 VERIFIED 证据必须指回根因证据与复测证据
    verified = [item for item in env_platform.store.all() if item.kind.value == "verified"]
    assert verified and verified[-1].derived_from


def test_denied_decision_is_never_executed(env_platform):
    manager = _supported(env_platform)
    decision = env_platform.recovery.evaluate(
        manager, attempted=["refresh_environment_state", "reallocate_environment"]
    )
    assert not decision.approved
    outcome = env_platform.recovery.execute(
        decision, env_platform.tool_ctx, _usage()
    )
    assert outcome.executed is False
    assert "NEEDS_APPROVAL" in outcome.note


def test_rerun_passed_and_root_cause_are_separate_facts(env_platform):
    """§7.2：复测通过只证明当前环境下问题不复现，不代表根因已被独立确认。"""
    manager = _supported(env_platform)
    decision = env_platform.recovery.evaluate(
        manager, attempted=["refresh_environment_state"]
    )
    usage = _usage()
    outcome = env_platform.recovery.execute(decision, env_platform.tool_ctx, usage)
    outcome = env_platform.recovery.verify(
        outcome, env_platform.tool_ctx, usage, root_cause_evidence=["EV_1"]
    )
    assert outcome.rerun_passed is True
    assert "根因结论仍需结合恢复前后的差异" in outcome.note


# ======================================================================
# 端到端行为
# ======================================================================
def test_ladder_escalates_from_r1_to_r2(env_platform):
    result = env_platform.troubleshoot(use_templates=False)
    assert result.terminal_state is TerminalState.RESOLVED
    assert [item.action for item in result.recovery] == [
        "refresh_environment_state",
        "reallocate_environment",
    ]


def test_r3_blocks_at_human_when_not_approved(make_platform):
    platform = make_platform("device-hung")
    result = platform.troubleshoot(use_templates=False)
    assert result.terminal_state is TerminalState.NEEDS_HUMAN
    assert result.root_cause_key == "H_ENV"          # 已经定位，只是不能自动动手
    assert "restart_device" not in [item.action for item in result.recovery]


def test_r3_resolves_after_approval(make_platform):
    platform = make_platform("device-hung")
    result = platform.troubleshoot(use_templates=False, auto_approve=True)
    assert result.terminal_state is TerminalState.RESOLVED
    assert "restart_device" in [item.action for item in result.recovery]
    assert platform.recovery.approved_actions == {"restart_device"}


def test_no_automated_recovery_yields_root_cause_identified(make_platform):
    platform = make_platform("config")
    result = platform.troubleshoot(use_templates=False)
    assert result.terminal_state is TerminalState.ROOT_CAUSE_IDENTIFIED
    assert result.recovery == []
    assert result.root_cause_key == "H_CONFIG"
