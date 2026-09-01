"""示例 1：Agent 不再写死配置（设计说明书 §1）。

之前：
    SYSTEM_PROMPT = "你是一个专业助手..."
    MODEL = "qwen..."
    TEMPERATURE = 0.7

之后：
    config = await config_service.resolve(agent, environment, user_id)

运行：
    python examples/agent.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import RegistryConfig
from app.factory import build_runtime


async def main() -> None:
    rt = await build_runtime(RegistryConfig(storage_backend="memory", cache_backend="memory"))
    pr, cr, pub, rsv = (
        rt.prompt_registry,
        rt.config_registry,
        rt.publisher,
        rt.resolver,
    )

    # ---- 0. 准备：Prompt / Config / 部署 --------------------------------------
    await pr.create_prompt("test_case_agent", created_by="alice")
    await pr.create_version(
        "test_case_agent",
        template="你是一名5G测试专家，请根据{requirement}给出测试用例。",
        variables=["requirement", "context"],
        created_by="alice",
    )
    await pr.create_version(
        "test_case_agent",
        template="你是一名5G测试专家，请详细给出测试用例并附带前置条件与预期结果。",
        variables=["requirement", "context"],
        created_by="alice",
    )
    await cr.create_config(
        "test_case_agent",
        model={"provider": "qwen", "name": "qwen3.5-27b"},
        parameters={"temperature": 0.2, "max_tokens": 4096},
        prompt={"name": "test_case_agent", "version": 1},
        tools={"version": 3},
        created_by="alice",
    )
    await pub.publish("test_case_agent", "prod", 1, created_by="alice")

    # ---- 1. Agent Runtime：拿到配置后直接执行 ----------------------------------
    user_id = "user_123"
    snapshot = await rsv.resolve(agent="test_case_agent", environment="prod", user_id=user_id)

    print("=" * 64)
    print("Agent 运行时拿到的配置快照（不再硬编码）：")
    print("=" * 64)
    data = snapshot.to_dict()
    print(f"  agent          : {data['agent']}")
    print(f"  config_version : v{data['config_version']}")
    print(f"  model          : {data['model']['provider']} / {data['model']['name']}")
    print(f"  temperature    : {data['parameters']['temperature']}")
    print(f"  prompt_version : {data['prompt']['version']}")
    print(f"  prompt         : {data['prompt']['template']}")
    print(f"  tools_version  : {data['tools']['version']}")
    print(f"  routing        : {data['routing']}")
    print()
    print(f"  execution_identity: {snapshot.execution_identity()}")
    print()
    print("  -> Semantic Cache 的 Key 应该包含这个 execution_identity（§33），")
    print("     这样 Prompt 升级后绝不会命中旧 Prompt 产生的缓存答案。")

    await rt.stop()


if __name__ == "__main__":
    asyncio.run(main())
