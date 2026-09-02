"""Argument Validator（设计说明书 §21）。

推荐 Pydantic；这里用同一份 JSON Schema 子集做「确定性校验」（§39），
对 tools.yaml 里出现的 schema 足够：类型 / required / pattern / 长度 / 范围 / enum。
pattern 上可自定义 message，用于表达资源边界（如路径必须位于 /tmp 下）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .schema_rule import SchemaIssue, validate

if TYPE_CHECKING:
    from ..tools.registry import ToolPolicy


class ArgumentValidator:
    def __init__(self, additional_properties_default: bool = True) -> None:
        # 遵循 JSON Schema 语义：未显式声明 additionalProperties:false 时允许额外键。
        # 需要严格约束（LLM 幻觉传参）的 Tool 在 tools.yaml 里显式声明 false。
        self.additional_properties_default = additional_properties_default

    def validate(
        self, arguments: dict | None, policy: "ToolPolicy"
    ) -> list[SchemaIssue]:
        """校验 arguments 是否符合该 tool 的参数 Schema。空 schema = 不限制。"""
        if not arguments:
            arguments = {}
        if not isinstance(arguments, dict):
            return [SchemaIssue("arguments", "expected an object")]
        schema = policy.schema
        if not schema:
            return []
        return validate(
            schema,
            arguments,
            path="arguments",
            additional_properties_default=self.additional_properties_default,
        )

    def first_error(self, arguments: dict | None, policy: "ToolPolicy") -> str | None:
        issues = self.validate(arguments, policy)
        return str(issues[0]) if issues else None


__all__ = ["ArgumentValidator"]
