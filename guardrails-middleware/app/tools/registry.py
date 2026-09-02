"""Tool Allowlist / Registry（设计说明书 §18、§20 第一层）。

第一层防线：tool 不在 Registry 内 -> BLOCK。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolPolicy:
    name: str
    allowed_agents: list[str] = field(default_factory=list)
    risk_level: str = "LOW"
    require_approval: bool = False
    description: str = ""
    schema: dict | None = None          # 参数 JSON Schema（§21 Argument Validator 用）
    resource_boundary: dict | None = None  # 预留：资源边界（文件系统/网络等）


@dataclass
class ToolRegistry:
    tools: dict[str, ToolPolicy] = field(default_factory=dict)

    def register(self, policy: ToolPolicy) -> "ToolRegistry":
        self.tools[policy.name] = policy
        return self

    def get(self, name: str) -> ToolPolicy | None:
        return self.tools.get(name)

    def has(self, name: str) -> bool:
        return name in self.tools

    def names(self) -> list[str]:
        return sorted(self.tools)

    def __contains__(self, name: str) -> bool:
        return name in self.tools


__all__ = ["ToolPolicy", "ToolRegistry"]
