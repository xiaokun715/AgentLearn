"""测试辅助函数。"""
from __future__ import annotations

from typing import Any


def parse_metric(text: str, name: str) -> float:
    """从 Prometheus 文本中读取某个 counter 的数值。"""
    for line in text.splitlines():
        if line.startswith(name + " ") or line.startswith(name + "{"):
            return float(line.split()[-1])
    return 0.0


def parse_sse(text: str) -> list[dict[str, Any]]:
    """把 SSE 原始文本解析为事件列表（含心跳 comment）。"""
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev: dict[str, Any] = {"id": None, "event": None, "data": None, "comment": False}
        for line in block.split("\n"):
            if line.startswith(":"):
                ev["comment"] = True
            elif line.startswith("id: "):
                ev["id"] = line[4:].strip()
            elif line.startswith("event: "):
                ev["event"] = line[7:].strip()
            elif line.startswith("data: "):
                ev["data"] = line[6:].strip()
        events.append(ev)
    return events
