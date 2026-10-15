"""诊断工具集合 —— 按领域拆成四个模块，统一在 :func:`build_registry` 里装配。

拆分的依据是**它们各自能产出哪一类证据**，而不是按代码行数：

- :mod:`~app.tools.log_tools`         日志 → 原始证据的主力
- :mod:`~app.tools.config_tools`      配置 / 用例定义 → 「怎么跑」与「判什么算过」
- :mod:`~app.tools.environment_tools` 环境 → 只读探针 + 三个分级的恢复动作
- :mod:`~app.tools.test_tools`        工具记录 / 复测 / 历史案例 / 缺陷单
"""
from __future__ import annotations

from . import config_tools, environment_tools, log_tools, test_tools
from .base import ToolCallRecord, ToolContext, ToolError, ToolRegistry


def build_registry() -> ToolRegistry:
    """装配全部诊断工具。"""
    registry = ToolRegistry()
    log_tools.register_all(registry)
    config_tools.register_all(registry)
    environment_tools.register_all(registry)
    test_tools.register_all(registry)
    return registry


__all__ = [
    "ToolRegistry",
    "ToolContext",
    "ToolError",
    "ToolCallRecord",
    "build_registry",
]
