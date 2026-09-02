"""Agent Permission（设计说明书 §20 第二层）。

第二层防线：tool 允许，但当前 agent 不在 ``allowed_agents`` -> BLOCK。
"""
from __future__ import annotations

from .registry import ToolPolicy


def check_agent_permission(agent: str, policy: ToolPolicy) -> tuple[bool, str]:
    """返回 (是否允许, 原因)。"""
    if not policy.allowed_agents:
        return True, ""
    if agent in policy.allowed_agents:
        return True, ""
    return False, (
        f"agent '{agent}' is not allowed to call tool '{policy.name}' "
        f"(allowed: {', '.join(policy.allowed_agents)})"
    )


__all__ = ["check_agent_permission"]
