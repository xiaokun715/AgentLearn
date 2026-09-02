"""Resource Boundary（设计说明书 §20 Resource Policy）。

工具参数里的路径类字段，除了 JSON Schema 的 ``pattern``，还必须做**规范化后**
的边界校验 —— 否则 ``/tmp/../etc/passwd`` 仍能绕过 ``^/tmp/.*`` 前缀正则
（review 修复：裸前缀正则可被路径穿越绕过）。

工具声明（tools.yaml）：
    resource_boundary:
      fields: [path]
      roots: ["/tmp"]
"""
from __future__ import annotations

import posixpath
from typing import Any


def is_within(rel_or_abs: str, root: str) -> bool:
    """规范化路径后判断是否位于 root 之下（绝对/相对均可）。"""
    path = posixpath.normpath(rel_or_abs.replace("\\", "/"))
    if path == ".." or path.startswith("../"):
        return False
    if path.startswith("/"):
        # 绝对路径：必须正好是 root，或 root/xxx
        if path == root:
            return True
        return path.startswith(root.rstrip("/") + "/")
    # 相对路径不允许逃逸根目录
    return not path.startswith("..") and not path.startswith("/")


def check_boundary(arguments: dict[str, Any], boundary: dict | None) -> list[str]:
    """对 boundary.fields 逐个校验是否位于 boundary.roots 内，返回违规消息列表。"""
    if not boundary:
        return []
    fields: list = boundary.get("fields", [])
    roots: list = boundary.get("roots", [])
    issues: list[str] = []
    for field in fields:
        value = arguments.get(field)
        if value is None:
            continue  # required 由 schema 负责
        if not isinstance(value, str):
            issues.append(f"resource boundary field '{field}' must be a string path")
            continue
        if not any(is_within(value, root) for root in roots):
            roots_txt = ", ".join(roots)
            issues.append(
                f"path '{value}' is outside allowed resource boundary ({roots_txt})"
            )
    return issues


__all__ = ["is_within", "check_boundary"]
