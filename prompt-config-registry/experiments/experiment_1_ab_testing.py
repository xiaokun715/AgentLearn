"""实验 1：A/B 测试 —— Prompt v1(简洁) vs v2(详细)（设计说明书 §36）。

流程：
  1. Prompt v1 "请简洁回答问题。"   → 输出短、慢、成功率略低
     Prompt v2 "请详细回答问题，并给出三个例子。" → 输出长、慢、成功率高
  2. Production 100% -> v1
  3. 灰度 v2 @ 10%（A/B）
  4. 模拟 1000 个请求，按 user 的 hash bucket 分到 Variant A/B
  5. 统计 Success / Latency / Tokens / Cost，对比后决定 Promote v2

运行：
    python experiments/experiment_1_ab_testing.py
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

AGENT = "test_case_agent"
INPUT_COST_PER_M = 1.0    # $ / 1M tokens
OUTPUT_COST_PER_M = 5.0   # $ / 1M tokens


def _simulate(config_version: int, user: str) -> dict:
    """确定性模拟一次 LLM 调用（同一 user + 版本结果一致，便于对比）。

    seed 必须混入 user —— 只混 question 的话，5 个问题只有 5 种结果，统计会失真。
    """
    seed = int(hashlib.sha256(f"{config_version}:{user}".encode()).hexdigest()[:8], 16) % 1000
    if config_version == 1:
        # 简洁回答：tokens 少、速度快、但偶发信息不足导致成功率略低
        success = seed % 100 < 82
        latency = 1.2 + (seed % 10) * 0.02
        output_tokens = 300 + (seed % 200)
    else:
        # 详细回答：tokens 多、速度慢、但更完整，成功率高
        success = seed % 100 < 88
        latency = 1.8 + (seed % 10) * 0.03
        output_tokens = 600 + (seed % 400)
    input_tokens = 150 + (seed % 50)
    cost = input_tokens / 1e6 * INPUT_COST_PER_M + output_tokens / 1e6 * OUTPUT_COST_PER_M
    return {
        "success": success,
        "latency": latency,
        "tokens": input_tokens + output_tokens,
        "cost": cost,
    }


async def main() -> None:
    rt = await build_runtime(RegistryConfig(storage_backend="memory", cache_backend="memory"))
    pr, cr, pub, rsv = (
        rt.prompt_registry,
        rt.config_registry,
        rt.publisher,
        rt.resolver,
    )

    # 1) 两个 Prompt 版本
    await pr.create_prompt(AGENT, created_by="alice")
    await pr.create_version(AGENT, template="请简洁回答问题。", created_by="alice")
    await pr.create_version(AGENT, template="请详细回答问题，并给出三个例子。", created_by="alice")
    await cr.create_config(AGENT, prompt={"name": AGENT, "version": 1}, created_by="alice")
    await cr.create_config(AGENT, prompt={"name": AGENT, "version": 2}, created_by="alice")

    # 2) 100% -> v1，然后 canary v2 @ 10%
    await pub.publish(AGENT, "prod", 1, created_by="alice")
    await pub.publish(AGENT, "prod", 2, traffic_percent=10, experiment="prompt_v2_test",
                      created_by="alice", reason="A/B 测试")

    # 3) 模拟 1000 个请求
    N = 1000
    results: dict[int, list[dict]] = {1: [], 2: []}
    for i in range(N):
        user = f"user_{i}"
        snap = await rsv.resolve(AGENT, "prod", user)
        version = snap.config_version
        results[version].append(_simulate(version, user))

    # 4) 统计
    print("=" * 72)
    print(f"A/B 实验结果（{N} 个请求, experiment=prompt_v2_test）")
    print("=" * 72)
    print(f"{'':12}{'v1 (简洁)':>16}{'v2 (详细)':>16}")
    print("-" * 72)
    for key, label in (("success", "Success"), ("latency", "Latency(s)"),
                       ("tokens", "Tokens"), ("cost", "Cost($)")):
        fmt = "{:.2f}" if key == "cost" else "{:.1f}"
        row = []
        for v in (1, 2):
            vals = results[v]
            if not vals:
                row.append("-")
                continue
            if key == "success":
                row.append(f"{mean(r[key] for r in vals) * 100:.0f}%")
            elif key == "latency":
                row.append(f"{mean(r[key] for r in vals):.2f}")
            elif key == "tokens":
                row.append(f"{mean(r[key] for r in vals):.0f}")
            else:
                row.append(f"${mean(r[key] for r in vals):.4f}")
        print(f"  {label:<12}{row[0]:>16}{row[1]:>16}")
    share = len(results[2]) / N
    print(f"  流量占比    v1={len(results[1])/N:.1%}  v2={share:.1%}")
    print()

    # 5) 决策：v2 成功率更高 -> 全量发布
    v2_success = mean(r["success"] for r in results[2]) if results[2] else 0
    v1_success = mean(r["success"] for r in results[1]) if results[1] else 0
    if v2_success > v1_success:
        dep = await pub.rollout(
            (await rt.repo.get_deployment(AGENT, "prod")).id, 2, 100,
            created_by="alice", reason="A/B 结论：v2 Success 更高，全量发布",
        )
        print(f"结论：v2 Success({v2_success:.0%}) > v1({v1_success:.0%})，全量发布 -> {dep.status}")
    else:
        print(f"结论：v2 未胜出，保持 v1，实验继续 / 或回滚")
        await rt.rollback_service.rollback(
            (await rt.repo.get_deployment(AGENT, "prod")).id,
            created_by="alice", reason="A/B 未胜出，回滚",
        )

    await rt.stop()


if __name__ == "__main__":
    asyncio.run(main())
