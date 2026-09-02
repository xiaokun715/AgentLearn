"""Output Validator 系列（设计说明书 §26）—— Output 不只是文本。

Agent Output 可能是 Text / JSON / Tool Call / File / URL / SQL / Code。
本模块提供各类型的「确定性校验」辅助，供调用方/上层编排使用；
JSON Schema 的结构校验由 SchemaDetector 在 OUTPUT 阶段完成（→ RETRY，Demo 8）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .schema_rule import SchemaIssue, validate


@dataclass
class OutputIssue:
    kind: str          # TEXT / JSON / URL / FILE ...
    message: str
    path: str = ""

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}" if not self.path else f"[{self.kind}] {self.path}: {self.message}"


@dataclass
class ValidatedOutput:
    issues: list[OutputIssue] = field(default_factory=list)
    data: Any = None            # JSON 场景解析后的对象

    @property
    def ok(self) -> bool:
        return not self.issues


class OutputValidators:
    """纯静态方法集合：逐类校验，互相独立，方便组合成 OutputGuard。"""

    @staticmethod
    def validate_text(content: Any, *, min_len: int = 0, max_len: int = 100_000) -> list[OutputIssue]:
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        issues: list[OutputIssue] = []
        if len(text) < min_len:
            issues.append(OutputIssue("TEXT", f"shorter than min_len {min_len}"))
        if len(text) > max_len:
            issues.append(OutputIssue("TEXT", f"longer than max_len {max_len}"))
        return issues

    @staticmethod
    def validate_json(content: Any, schema: dict | None = None) -> ValidatedOutput:
        """解析 JSON 并按可选 schema 校验。"""
        value = content
        if isinstance(content, str):
            try:
                value = json.loads(content)
            except json.JSONDecodeError as exc:
                return ValidatedOutput(
                    issues=[OutputIssue("JSON", f"not valid JSON: {exc}")], data=None
                )
        if schema:
            issues = validate(schema, value)
            return ValidatedOutput(
                issues=[OutputIssue("JSON", i.message, i.path) for i in issues], data=value
            )
        return ValidatedOutput(issues=[], data=value)

    @staticmethod
    def validate_url(content: str, *, allowed_schemes: tuple[str, ...] = ("http", "https")) -> list[OutputIssue]:
        try:
            parsed = urlparse(content)
        except Exception as exc:  # noqa: BLE001
            return [OutputIssue("URL", f"not parseable: {exc}")]
        if parsed.scheme not in allowed_schemes:
            return [OutputIssue("URL", f"scheme '{parsed.scheme}' not allowed")]
        if not parsed.netloc:
            return [OutputIssue("URL", "missing host")]
        return []

    @staticmethod
    def validate_file(content: Any) -> list[OutputIssue]:
        """Demo 占位：文件型输出必须附大小与 MIME，超出阈值告警。"""
        size = len(content) if isinstance(content, (bytes, str)) else 0
        if size > 10 * 1024 * 1024:
            return [OutputIssue("FILE", "output file larger than 10 MiB")]
        return []


__all__ = ["OutputIssue", "ValidatedOutput", "OutputValidators"]
