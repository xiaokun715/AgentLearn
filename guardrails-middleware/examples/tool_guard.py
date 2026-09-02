"""示例：Tool Guardrail（设计说明书 §31、Demo 4 / 5 / 7）。

    python examples/tool_guard.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 保证从任意位置执行都能 import 到本项目的 app（避免与兄弟 demo 的 app 包冲突）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.factory import build_guardrails  # noqa: E402


async def main() -> None:
    g = build_guardrails()

    # Demo 4：Agent 无权 -> BLOCK
    r = await g.check_tool("fault_diagnosis", "delete_file", {"path": "/tmp/x.log"})
    print(f"[permission] action={r.action.value} risk={r.risk_level}")
    print(f"              reason = {r.reason}")

    # Demo 5：参数越界 -> BLOCK
    r = await g.check_tool("environment_recovery", "delete_file", {"path": "/etc/passwd"})
    print(f"[argument]   action={r.action.value} risk={r.risk_level}")
    print(f"              reason = {r.reason}")

    # Demo 7：高风险需人工审批 -> 创建票据 -> approve
    r = await g.check_tool("environment_recovery", "execute_shell", {"command": "ls /tmp"})
    print(f"[approval]   action={r.action.value} approval_id={r.approval_id}")
    if r.approval_id:
        decided = g.decide_approval(r.approval_id, approved=True, decided_by="ops")
        print(f"              decided status = {decided['status']}")


if __name__ == "__main__":
    asyncio.run(main())
