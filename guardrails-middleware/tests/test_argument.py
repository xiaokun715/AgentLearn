"""ArgumentValidator 单元测试（设计说明书 §21）。"""
from __future__ import annotations

from app.tools.registry import ToolPolicy
from app.validators.argument import ArgumentValidator

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
