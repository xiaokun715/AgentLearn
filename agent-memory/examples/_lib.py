"""CLI 演示脚本共用的小工具：UTF-8 输出、标题、表格打印。"""
from __future__ import annotations

import sys
from typing import Iterable


def enable_utf8() -> None:
    """Windows 控制台默认 GBK 会打乱中文，尽量切到 UTF-8(失败就静默)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(10, 60 - len(title)))


def show(rows: Iterable[str]) -> None:
    for r in rows:
        print("   " + r)
