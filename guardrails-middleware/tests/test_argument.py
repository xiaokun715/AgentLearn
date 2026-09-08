"""ArgumentValidator 单元测试（设计说明书 §21）。"""
from __future__ import annotations

from app.core.decision import Action
from app.tools.registry import ToolPolicy
from app.tools.resource import is_within
from app.tools.risk import RiskPolicy
from app.validators.argument import ArgumentValidator
from app.validators.schema_rule import validate

DEL_SCHEMA = {
    "type": "object",
    "required": ["path"],
    "additionalProperties": False,
    "properties": {
        "path": {
            "type": "string",
            "pattern": r"^/tmp/.*",
            "message": "Path is outside allowed resource boundary",
        }
    },
}


def test_invalid_path_blocked():
    validator = ArgumentValidator()
    policy = ToolPolicy(name="delete_file", schema=DEL_SCHEMA)
    err = validator.first_error({"path": "/etc/passwd"}, policy)
    assert err is not None
    assert "resource boundary" in err


def test_valid_path_passes():
    validator = ArgumentValidator()
    policy = ToolPolicy(name="delete_file", schema=DEL_SCHEMA)
    assert validator.first_error({"path": "/tmp/app.log"}, policy) is None


def test_unknown_field_rejected():
    validator = ArgumentValidator()
    policy = ToolPolicy(name="delete_file", schema=DEL_SCHEMA)
    err = validator.first_error({"path": "/tmp/x", "extra": 1}, policy)
    assert err is not None
    assert "extra" in err


def test_type_and_required_checks():
    validator = ArgumentValidator()
    policy = ToolPolicy(name="t", schema={
        "type": "object",
        "required": ["name"],
        "additionalProperties": False,
        "properties": {"name": {"type": "string"}, "age": {"type": "integer", "maximum": 150}},
    })
    assert validator.first_error({}, policy)
    assert validator.first_error({"name": 3}, policy)
    assert validator.first_error({"name": "a", "age": 200}, policy)
    assert validator.first_error({"name": "a", "age": 30}, policy) is None


def test_resource_boundary_normalized():
    # review 修复 #4：不能只靠 ^/tmp/.* 前缀，需 normpath 规范化
    assert is_within("/tmp/app.log", "/tmp") is True
    assert is_within("/tmp/a/../b", "/tmp") is True      # 规范化后仍在 /tmp 内
    assert is_within("/tmp/../etc/passwd", "/tmp") is False
    assert is_within("/etc/passwd", "/tmp") is False
    assert is_within("../../etc/passwd", "/tmp") is False


def test_risk_policy_missing_level_falls_back():
    # review 修复 #9：YAML 漏配某个风险等级时，回落到保守默认，而不是变 ALLOW
    rp = RiskPolicy(mappings={})
    assert rp.action_for_risk("CRITICAL") == Action.HUMAN_APPROVAL
    assert rp.action_for_risk("HIGH") == Action.HUMAN_APPROVAL


def test_schema_number_accepts_integer():
    # JSON Schema：integer 值满足 number 约束（review 修复 #5）
    assert validate({"type": "number", "minimum": 1}, 5) == []
    assert validate({"type": "number", "minimum": 1}, 1.5) == []
    # 仍会执行范围校验（0.5 < 1 -> 报错）
    assert validate({"type": "number", "minimum": 1}, 0.5) != []


def test_schema_additional_properties_default_true():
    # 遵循 JSON Schema 语义：未声明 additionalProperties 时允许额外键（review 修复 #6）
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert validate(schema, {"a": "x", "b": 1}) == []
    strict = {"type": "object", "additionalProperties": False,
              "properties": {"a": {"type": "string"}}}
    assert validate(strict, {"a": "x", "b": 1})
