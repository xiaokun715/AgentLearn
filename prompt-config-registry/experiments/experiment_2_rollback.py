"""实验 2：故障回滚 —— Prompt v10 正常, v11 导致 Tool Success 暴跌（设计说明书 §37）。

流程：
  1. Config v1（引用"严格调用"的 Prompt）：Tool Success ≈ 95%
  2. 发布 Config v2（引用"自由发挥"的 Prompt）：因为 prompt 改变工具调用行为，Success ≈ 65%
  3. 系统检测到指标异常（Success < 阈值）
  4. 自动 Rollback -> v1，指标恢复

关键点：Rollback 不是删除 v2 —— 版本还在，只是路由回 v1。

运行：
    python experiments/experiment_2_rollback.py
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from statistics import mean

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import RegistryConfig
from app.factory import build_runtime

AGENT = "tool_agent"
SUCCESS_THRESHOLD = 0.85  # 自动回滚阈值

TOOL_TASKS = [
    "search_kb_5g_protocol",
    "calculator_coverage",
    "database_cell_params",
    "search_kb_handover",
    "simulator_interference",
]


def _tool_success(config_version: int, task: str, i: int) -> bool:
    """确定性模拟：v1 正常（95%），v2 工具调用退化（65%）。

    seed 混入请求序号 i，保证统计量真实。
    """
    seed = int(hashlib.sha256(f"{config_version}:{task}:{i}".encode()).hexdigest()[:8], 16) % 100
    if config_version == 1:
        return seed < 95
    return seed < 65


async def _measure(runtime, config_version: int, n: int) -> float:
    success = [_tool_success(config_version, TOOL_TASKS[i % len(TOOL_TASKS)], i) for i in range(n)]
    return mean(success)


async def main() -> None:
    rt = await build_runtime(RegistryConfig(storage_backend="memory", cache_backend="memory"))
    pr, cr, pub, roll = (
        rt.prompt_registry,
        rt.config_registry,
        rt.publisher,
        rt.rollback_service,
    )

    # v1 = 严格按工具说明调用（正常）；v2 = 自由发挥（有问题的 prompt）
    await pr.create_prompt(AGENT, created_by="alice")
    await pr.create_version(AGENT, template="你是工具调用专家，请严格按工具说明调用。", created_by="alice")
    await pr.create_version(AGENT, template="你是工具调用专家，请自由发挥，尽可能多地调用工具。", created_by="alice")
    c1 = await cr.create_config(AGENT, prompt={"name": AGENT, "version": 1}, created_by="alice")
    c2 = await cr.create_config(AGENT, prompt={"name": AGENT, "version": 2}, created_by="alice")

    print("=" * 72)
    print("故障回滚演练（Config v1 正常 -> 发布 v2 -> 自动回滚 -> v1）")
    print("=" * 72)

    baseline = await _measure(rt, c1.version, 300)
    print(f"1. 基线  : Config v{c1.version} Tool Success = {baseline:.0%}")

    # v1 已在生产运行（§37：Prompt v10 正常 -> 再发布 v11）
    await pub.publish(AGENT, "prod", c1.version, created_by="alice", reason="初始发布 v1")

    dep = await pub.publish(AGENT, "prod", c2.version, created_by="alice", reason="上线 v2")
    print(f"2. 发布  : {' '.join(f'v{r.version}:{r.weight}%' for r in dep.rules)} [{dep.status}]")

    degraded = await _measure(rt, c2.version, 300)
    print(f"3. 观测  : Config v{c2.version} Tool Success = {degraded:.0%}  (阈值 {SUCCESS_THRESHOLD:.0%})")

    if degraded < SUCCESS_THRESHOLD:
        print("4. 告警  : Success 跌破阈值，触发自动回滚 ...")
        rolled = await roll.rollback(dep.id, created_by="auto-rollback", reason="tool success 暴跌")
        print(f"   -> 回滚: {' '.join(f'v{r.version}:{r.weight}%' for r in rolled.rules)} [{rolled.status}]")
        recovered = await _measure(rt, c1.version, 300)
        print(f"5. 恢复  : Config v{c1.version} Tool Success = {recovered:.0%}")
    else:
        print("4. 指标正常，无需回滚。")

    # v2 仍然存在（回滚不是删除版本）
    v2 = await rt.config_registry.require_config(AGENT, c2.version)
    print(f"6. 验证  : Config v{v2.version} 仍在（引用 Prompt v{v2.prompt.version}）—— 只是不再路由。")
    await rt.stop()


if __name__ == "__main__":
    asyncio.run(main())
