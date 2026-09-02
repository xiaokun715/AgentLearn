"""策略 YAML 加载器（设计说明书 §5 configs/）。

- ``policies.yaml`` -> PolicyEngine（stage -> category -> action）
- ``tools.yaml``    -> ToolRegistry + RiskPolicy（§18、§20）
- ``guardrails.yaml`` -> 脱敏模板（category -> 遮罩）等运行参数
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ..core.decision import Action
from ..core.exceptions import ConfigError
from .engine import PolicyEngine
from .models import PolicyRule, PolicyTable


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid yaml in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping: {path}")
    return data


def load_policy_engine(path: Path) -> PolicyEngine:
    """从 policies.yaml 构建 PolicyEngine。"""
    data = _read_yaml(path)
    table: PolicyTable = {}
    stages = data.get("stages", {})
    if not isinstance(stages, dict):
        raise ConfigError(f"{path}: 'stages' must be a mapping")

    for stage_name, category_map in stages.items():
        if not isinstance(category_map, dict):
            continue
        table[str(stage_name).upper()] = {}
        for category, rule in category_map.items():
            if isinstance(rule, str):  # 允许简写：CATEGORY: BLOCK
                rule = {"action": rule}
            if not isinstance(rule, dict) or "action" not in rule:
                raise ConfigError(f"{path}: rule for {stage_name}.{category} missing 'action'")
            try:
                action = Action(str(rule["action"]).lower())
            except ValueError as exc:
                raise ConfigError(
                    f"{path}: unknown action {rule['action']!r} for {stage_name}.{category}"
                ) from exc
            params = {k: v for k, v in rule.items() if k != "action"}
            table[str(stage_name).upper()][str(category)] = PolicyRule(action=action, params=params)

    defaults = data.get("defaults", {}) if isinstance(data.get("defaults"), dict) else {}
    default_action = Action(str(defaults.get("action", "allow")).lower())
    return PolicyEngine(table, default_action)


def load_redactions(path: Path) -> dict[str, str]:
    """从 guardrails.yaml 加载 category -> 脱敏模板。"""
    data = _read_yaml(path)
    redactions = data.get("redactions", {})
    return dict(redactions) if isinstance(redactions, dict) else {}


def load_config_yaml(path: Path) -> dict:
    return _read_yaml(path)


__all__ = ["load_policy_engine", "load_redactions", "load_config_yaml"]
