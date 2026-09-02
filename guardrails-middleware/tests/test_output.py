"""Output Guardrail / Schema -> RETRY 测试（设计说明书 §26~§27、Demo 8）。"""
from __future__ import annotations

from app.validators.output import OutputValidators

# 期望 Agent 输出（§26 示例：case_id 必须为字符串）
OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["status", "case_id"],
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string"},
        "case_id": {"type": "string"},
    },
}


async def test_json_schema_mismatch_retry(g):
    # case_id 传成了 int -> SchemaDetector 命中 SCHEMA_MISMATCH -> OUTPUT 策略 RETRY
    r = await g.check_output({"status": "success", "case_id": 123}, schema=OUTPUT_SCHEMA)
    assert r.action.value == "retry"
    assert r.blocked is False
    assert r.retry_guidance
    assert "case_id" in r.retry_guidance
    assert "SCHEMA_MISMATCH" in {f.category for f in r.findings}


async def test_json_schema_valid_allow(g):
    r = await g.check_output(
        {"status": "success", "case_id": "TC001"}, schema=OUTPUT_SCHEMA
    )
    assert r.action.value == "allow"
    assert r.blocked is False


async def test_invalid_json_retry(g):
    r = await g.check_output('{"status": broken', schema=OUTPUT_SCHEMA)
    assert r.action.value == "retry"


async def test_text_output_without_schema_allow(g):
    r = await g.check_output("排查完成，故障已定位。")
    assert r.action.value == "allow"


def test_output_validators_text_and_url():
    assert OutputValidators.validate_text("short") == []
    assert OutputValidators.validate_text("", min_len=1)
    assert OutputValidators.validate_url("javascript:alert(1)")
    assert OutputValidators.validate_url("https://example.com") == []
