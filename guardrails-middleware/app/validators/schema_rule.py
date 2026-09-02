"""JSON Schema 子集校验器 —— 纯确定性规则（不用 LLM / Pydantic 动态模型）。

支持的子集（覆盖本项目 configs/*.yaml 中出现的 schema）：
    type / properties / required / additionalProperties
    items / minItems / maxItems
    pattern / minLength / maxLength
    minimum / maximum / exclusiveMinimum / exclusiveMaximum
    enum / const

用法：
    issues = validate({"type": "string", "pattern": "^/tmp/.*"}, "/etc/passwd")
    issues[0].message  # -> "path: string does not match pattern ..."
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SchemaIssue:
    path: str          # 形如 "arguments.path" / "case_id"
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def _type_name(t: str) -> str:
    return {"object": "object", "array": "array", "string": "string",
            "number": "number", "integer": "integer", "boolean": "boolean"}.get(t, t)


def validate(
    schema: dict,
    value,
    path: str = "",
    additional_properties_default: bool = True,
) -> list[SchemaIssue]:
    """对 ``value`` 按 ``schema`` 校验，返回问题列表（空 = 合法）。

    ``additionalProperties`` 遵循 JSON Schema 语义：未声明时默认允许额外键；
    需要收紧时在 schema 里显式声明 ``additionalProperties: false``。
    """
    issues: list[SchemaIssue] = []
    if not isinstance(schema, dict):
        return issues

    # enum / const 先行
    if "enum" in schema and value not in schema["enum"]:
        issues.append(SchemaIssue(path, f"value not in enum {schema['enum']}"))
    if "const" in schema and value != schema["const"]:
        issues.append(SchemaIssue(path, f"value != const {schema['const']!r}"))

    expected = schema.get("type")
    if expected is None:
        _validate_generic(issues, schema, value, path, additional_properties_default)
        return issues

    actual = _actual_type(value)
    # JSON Schema：number 满足 integer，因此 integer 值也满足 number 约束
    if not ((expected == "number" and actual == "integer") or actual == expected):
        issues.append(SchemaIssue(path, f"expected {_type_name(expected)}, got {_type_name(actual)}"))
        return issues  # 类型错时不再深入

    if expected == "object":
        _validate_object(issues, schema, value, path, additional_properties_default)
    elif expected == "array":
        _validate_array(issues, schema, value, path)
    elif expected == "string":
        _validate_string(issues, schema, value, path)
    elif expected in ("integer", "number"):
        _validate_number(issues, schema, value, path, expected == "integer")
    return issues


def _actual_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null" if value is None else type(value).__name__


def _validate_generic(
    issues: list, schema: dict, value, path: str, addl_default: bool = True
) -> None:
    """无 type 声明时仍校验可用的约束。"""
    if isinstance(value, str):
        _validate_string(issues, schema, value, path)
    elif isinstance(value, int) and not isinstance(value, bool):
        _validate_number(issues, schema, value, path, integer=True)
    elif isinstance(value, float):
        _validate_number(issues, schema, value, path, integer=False)
    elif isinstance(value, list):
        _validate_array(issues, schema, value, path)
    elif isinstance(value, dict):
        _validate_object(issues, schema, value, path, addl_default)


def _validate_object(
    issues: list, schema: dict, value: dict, path: str, addl_default: bool
) -> None:
    properties: dict = schema.get("properties", {})
    required: list = schema.get("required", [])

    for name in required:
        if name not in value:
            issues.append(SchemaIssue(f"{path}.{name}" if path else name, "is required"))

    for name, prop in properties.items():
        if name not in value:
            continue
        sub = f"{path}.{name}" if path else name
        issues.extend(validate(prop, value[name], sub, addl_default))

    addl: dict | bool = schema.get("additionalProperties", addl_default)
    allowed = set(properties) | set(required)
    if addl is False:
        for key in value:
            if key not in allowed:
                issues.append(
                    SchemaIssue(f"{path}.{key}" if path else key, "is not allowed")
                )
    elif isinstance(addl, dict):
        for key, val in value.items():
            if key in allowed:
                continue
            sub = f"{path}.{key}" if path else key
            issues.extend(validate(addl, val, sub, addl_default))


def _validate_array(issues: list, schema: dict, value: list, path: str) -> None:
    if "minItems" in schema and len(value) < schema["minItems"]:
        issues.append(SchemaIssue(path, f"expected >= {schema['minItems']} items, got {len(value)}"))
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        issues.append(SchemaIssue(path, f"expected <= {schema['maxItems']} items, got {len(value)}"))
    if "items" in schema:
        items_schema = schema["items"]
        for i, item in enumerate(value):
            issues.extend(validate(items_schema, item, f"{path}[{i}]"))


def _validate_string(issues: list, schema: dict, value: str, path: str) -> None:
    import re

    if "minLength" in schema and len(value) < schema["minLength"]:
        issues.append(SchemaIssue(path, f"shorter than minLength {schema['minLength']}"))
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        issues.append(SchemaIssue(path, f"longer than maxLength {schema['maxLength']}"))
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str) or len(pattern) > 200:
            issues.append(SchemaIssue(path, "invalid or unsafe pattern"))
        else:
            try:
                matched = re.search(pattern, value) is not None
            except re.error:
                matched = True  # 已单独报 invalid pattern，避免重复 mismatch
                issues.append(SchemaIssue(path, f"invalid pattern: {pattern}"))
            if not matched:
                issues.append(
                    SchemaIssue(
                        path,
                        schema.get("message", f"does not match pattern {pattern}"),
                    )
                )


def _validate_number(issues: list, schema: dict, value, path: str, integer: bool) -> None:
    if integer and isinstance(value, float):
        # 此时 value 只可能是整型；防御性保留，兼容 5.0 这类整值浮点
        if value.is_integer():
            value = int(value)
        else:
            issues.append(SchemaIssue(path, "expected integer, got float"))
            return
    if "minimum" in schema and value < schema["minimum"]:
        issues.append(SchemaIssue(path, f"less than minimum {schema['minimum']}"))
    if "maximum" in schema and value > schema["maximum"]:
        issues.append(SchemaIssue(path, f"greater than maximum {schema['maximum']}"))
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        issues.append(SchemaIssue(path, f"not greater than exclusiveMinimum {schema['exclusiveMinimum']}"))
    if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
        issues.append(SchemaIssue(path, f"not less than exclusiveMaximum {schema['exclusiveMaximum']}"))


__all__ = ["SchemaIssue", "validate"]
