"""tools.yaml 加载器（设计说明书 §18、§5 configs/tools.yaml）。

tools.yaml 顶层结构：
    tools:            # name -> ToolPolicy（allowed_agents / risk_level / require_approval / schema）
    risk_policies:    # RISK_LEVEL -> action（HIGH/CRITICAL 默认需要人工审批）
"""
from __future__ import annotations

from pathlib import Path

from ..core.decision import Action
from ..core.exceptions import ConfigError
from ..policies.loader import _read_yaml
from .registry import ToolPolicy, ToolRegistry
from .risk import RiskPolicy


def load_tools(path: Path) -> tuple[ToolRegistry, RiskPolicy]:
    data = _read_yaml(path)

    tools_raw = data.get("tools", {})
    if not isinstance(tools_raw, dict):
        raise ConfigError(f"{path}: 'tools' must be a mapping")

    registry = ToolRegistry()
    for name, spec in tools_raw.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"{path}: tool '{name}' must be a mapping")
        registry.register(
            ToolPolicy(
                name=str(name),
                description=str(spec.get("description", "")),
                allowed_agents=[str(a) for a in spec.get("allowed_agents", [])],
                risk_level=str(spec.get("risk_level", "LOW")),
                require_approval=bool(spec.get("require_approval", False)),
                schema=spec.get("schema"),
                resource_boundary=spec.get("resource_boundary"),
            )
        )

    risk_map = data.get("risk_policies", {})
    mappings: dict[str, Action] = {}
    if isinstance(risk_map, dict):
        for level, action in risk_map.items():
            try:
                mappings[str(level).upper()] = Action(str(action).lower())
            except ValueError as exc:
                raise ConfigError(f"{path}: unknown risk action {action!r}") from exc
    return registry, RiskPolicy(mappings=mappings)


__all__ = ["load_tools"]
