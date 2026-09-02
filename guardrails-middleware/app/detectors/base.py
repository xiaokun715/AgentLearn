"""Detector 抽象（设计说明书 §9）。

新增 Detector（SQL Injection / Malware / Copyright / …）不需要修改 Pipeline，
只需实现 ``detect`` 并声明适用阶段。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Pattern

from ..core.context import Stage

if TYPE_CHECKING:
    from ..core.context import GuardrailContext
    from ..core.finding import SecurityFinding

# 含自由文本的「内容阶段」
TEXT_STAGES = frozenset({Stage.INPUT, Stage.CONTEXT, Stage.TOOL_RESULT, Stage.OUTPUT})


def find_all(pattern: Pattern[str], text: str) -> list[tuple[str, int, int]]:
    """返回 [(原文, 起始, 结束)]，供 Finding.location 与 metadata["raw"] 使用。"""
    return [(m.group(0), m.start(), m.end()) for m in pattern.finditer(text)]


class Detector(ABC):
    """Detector 基类。"""

    name: str = "base"
    stages: frozenset[Stage] = frozenset()

    def applicable(self, stage: Stage) -> bool:
        return stage in self.stages

    @abstractmethod
    async def detect(self, context: "GuardrailContext") -> list["SecurityFinding"]:
        """返回发现的全部 Finding（无风险返回 []）。"""
        raise NotImplementedError


__all__ = ["Detector", "TEXT_STAGES", "find_all"]
