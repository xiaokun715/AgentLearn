"""Policy Engine 测试（设计说明书 §14~§15）。"""
from __future__ import annotations

from app.core.context import Stage
from app.core.decision import Action
from app.core.finding import SecurityFinding


def _finding(category: str) -> SecurityFinding:
    return SecurityFinding(
        detector="test", category=category, severity="HIGH",
        confidence=0.9, message=f"{category}",
    )


async def test_same_finding_different_stage_different_action(g):
    engine = g.policy_engine
    inj = _finding("PROMPT_INJECTION")
    # 直接注入 -> INPUT BLOCK；间接注入 -> TOOL_RESULT SANITIZE（§14 关键设计）
    assert engine.evaluate(Stage.INPUT, [inj]).action == Action.BLOCK
    assert engine.evaluate(Stage.TOOL_RESULT, [inj]).action == Action.SANITIZE


async def test_redact_cannot_override_block(g):
    engine = g.policy_engine
    # 同时命中 PHONE(REDACT) 与 SECRET(BLOCK)，最终必须取最高风险 BLOCK（§15）
    decision = engine.evaluate(
        Stage.INPUT, [_finding("PHONE"), _finding("SECRET")]
    )
    assert decision.action == Action.BLOCK
    applied = dict(decision.applied)
    assert applied["PHONE"] == "redact"
    assert applied["SECRET"] == "block"


async def test_unknown_category_defaults_to_allow(g):
    engine = g.policy_engine
    assert engine.action_for(Stage.INPUT, "NO_SUCH_RISK") == Action.ALLOW
    assert engine.evaluate(Stage.INPUT, []).action == Action.ALLOW


async def test_output_schema_maps_to_retry(g):
    engine = g.policy_engine
    decision = engine.evaluate(Stage.OUTPUT, [_finding("SCHEMA_MISMATCH")])
    assert decision.action == Action.RETRY
