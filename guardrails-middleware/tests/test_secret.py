"""Secret Detector 测试（设计说明书 §12、Demo 3）。"""
from __future__ import annotations

from app.core.context import GuardrailContext, Stage
from app.detectors.secret import SecretDetector


async def test_output_secret_block(g):
    # OUTPUT 阶段策略：SECRET -> BLOCK（不能把凭据发给用户，§25）
    r = await g.check_output("数据库账号密码是 admin / 123456，api_key 为 sk-" + "a" * 40)
    assert r.blocked is True
    cats = {f.category for f in r.findings}
    assert "SECRET" in cats
    assert r.content is None


async def test_tool_result_secret_redact(g):
    # TOOL_RESULT 阶段策略：SECRET -> REDACT（来自 Tool 的内容先脱敏再进 Context）
    key = "sk-" + "b" * 40
    r = await g.check_tool_result(f"网页返回中包含密钥 {key}，请参考。")
    assert r.action.value == "redact"
    assert key not in (r.content or "")
    assert "<SECRET_REDACTED>" in (r.content or "")


async def test_secret_subtypes():
    det = SecretDetector()
    ctx = GuardrailContext(
        request_id="r3", tenant_id="t", user_id="u", agent="a", stage=Stage.OUTPUT,
        content=(
            "openai key = sk-abcdefghijklmnopqrstuvwxyz0123456789 "
            "jwt = eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        ),
    )
    findings = await det.detect(ctx)
    assert findings
    assert all(f.category == "SECRET" for f in findings)
    subtypes = {f.metadata.get("subtype") for f in findings}
    assert {"OPENAI_KEY", "JWT"} <= subtypes
    assert all(f.severity == "CRITICAL" for f in findings)
