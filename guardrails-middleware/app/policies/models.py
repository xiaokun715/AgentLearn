"""策略模型（设计说明书 §14）。

关键设计：**同一个 Finding，在不同 Stage 可以拥有不同策略**。
例如 PROMPT_INJECTION 在 INPUT -> BLOCK，在 TOOL_RESULT -> SANITIZE。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.decision import Action

# 策略表：stage 名(大写) -> category -> PolicyRule
PolicyTable = dict[str, dict[str, "PolicyRule"]]


@dataclass
class PolicyRule:
    """某阶段某类风险应采取的动作，以及动作参数。"""

    action: Action
    params: dict[str, Any] = field(default_factory=dict)  # e.g. retry.max_retries


__all__ = ["PolicyRule", "PolicyTable"]
