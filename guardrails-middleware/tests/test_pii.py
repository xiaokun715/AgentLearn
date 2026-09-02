"""PII Detector 测试（设计说明书 §10、Demo 1）。"""
from __future__ import annotations

from app.core.context import GuardrailContext, Stage
from app.detectors.pii import PIIDetector


async def test_phone_input_redacted(g):
    r = await g.check_input("请联系我 13812345678 谢谢")
    assert r.action.value == "redact"
    assert r.blocked is False
    assert "13812345678" not in (r.content or "")
    assert "<PHONE_REDACTED>" in r.content
    assert "PHONE" in {f.category for f in r.findings}


async def test_email_redacted(g):
    r = await g.check_input("请发到 dev@example.com 邮箱")
    assert r.action.value == "redact"
    assert "dev@example.com" not in (r.content or "")
    assert "<EMAIL_REDACTED>" in r.content
    assert "EMAIL" in {f.category for f in r.findings}


async def test_detector_categories_and_severity():
    det = PIIDetector()
    ctx = GuardrailContext(
        request_id="r1", tenant_id="t", user_id="u", agent="a", stage=Stage.INPUT,
        content="身份证 11010519491231002X，银行卡 6222021234567890123",
    )
    findings = await det.detect(ctx)
    cats = {f.category for f in findings}
    assert {"ID_CARD", "BANK_CARD"} <= cats
    id_f = next(f for f in findings if f.category == "ID_CARD")
    assert id_f.severity == "HIGH"


async def test_no_pii_allow(g):
    r = await g.check_input("如何重置路由器？")
    assert r.action.value == "allow"
    assert r.blocked is False
    assert r.findings == []
