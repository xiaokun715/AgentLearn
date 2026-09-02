"""示例：Output Guardrail（设计说明书 §32、Demo 3 / 8）。

    python examples/output_guard.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 保证从任意位置执行都能 import 到本项目的 app（避免与兄弟 demo 的 app 包冲突）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.factory import build_guardrails  # noqa: E402

OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["status", "case_id"],
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string"},
        "case_id": {"type": "string"},
    },
}


async def main() -> None:
    g = build_guardrails()

    # Demo 3：LLM 输出想泄露凭据 -> BLOCK
    r = await g.check_output("数据库配置：api_key = sk-" + "e" * 40,
                             agent="fault_diagnosis")
    print(f"[leak]  action={r.action.value} blocked={r.blocked}")

    # Demo 8：输出不符合 JSON Schema -> RETRY（带修复建议给 LLM）
    r = await g.check_output({"status": "success", "case_id": 123},
                             schema=OUTPUT_SCHEMA, agent="fault_diagnosis")
    print(f"[schema] action={r.action.value} retry_guidance=\n{r.retry_guidance}")

    # 修正后再查 -> ALLOW
    r = await g.check_output({"status": "success", "case_id": "TC001"},
                             schema=OUTPUT_SCHEMA, agent="fault_diagnosis")
    print(f"[schema] action={r.action.value} blocked={r.blocked}")


if __name__ == "__main__":
    asyncio.run(main())
