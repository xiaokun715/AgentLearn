"""Schema Detector —— 输出结构校验（设计说明书 §26~§27、Demo 8）。

当上下文携带 ``metadata["schema"]`` 且 stage 为 OUTPUT 时，校验内容是否符合
JSON Schema；不符则产出 SCHEMA_MISMATCH Finding，由 POLICY 决定是否 RETRY。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..core.context import Stage
from ..core.finding import SecurityFinding
from ..validators.schema_rule import validate
from .base import Detector

if TYPE_CHECKING:
    from ..core.context import GuardrailContext


class SchemaDetector(Detector):
    name = "schema"
    stages = frozenset({Stage.OUTPUT})

    def __init__(self, max_messages: int = 3) -> None:
        self.max_messages = max_messages

    async def detect(self, context: "GuardrailContext") -> list[SecurityFinding]:
        schema = context.metadata.get("schema")
        if not isinstance(schema, dict):
            return []

        value = context.content
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return [
                    SecurityFinding(
                        detector=self.name,
                        category="SCHEMA_MISMATCH",
                        severity="MEDIUM",
                        confidence=1.0,
                        message="output is not valid JSON",
                        location="<root>",
                        metadata={"raw": None},
                    )
                ]

        issues = validate(schema, value)
        if not issues:
            return []

        # 只保留前 N 条，避免 LLM 一次看到海量报错
        findings: list[SecurityFinding] = []
        for issue in issues[: self.max_messages]:
            findings.append(
                SecurityFinding(
                    detector=self.name,
                    category="SCHEMA_MISMATCH",
                    severity="MEDIUM",
                    confidence=1.0,
                    message=f"schema mismatch at {issue.path}: {issue.message}",
                    location=issue.path,
                    metadata={"schema_path": issue.path},
                )
            )
        return findings


__all__ = ["SchemaDetector"]
