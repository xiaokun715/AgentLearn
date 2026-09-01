"""示例 2：完整生命周期 —— 创建 -> 环境绑定 -> 灰度 -> 全量 -> 回滚（设计说明书 §12~§18）。

运行：
    python examples/lifecycle.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import RegistryConfig
from app.factory import build_runtime

AGENT = "test_case_agent"


def _rules_str(dep) -> str:
    return ", ".join(f"v{r.version}:{r.weight}%" for r in dep.rules)


async def main() -> None:
    rt = await build_runtime(RegistryConfig(storage_backend="memory", cache_backend="memory"))
    pr, cr, pub, rsv, roll = (
        rt.prompt_registry,
        rt.config_registry,
        rt.publisher,
        rt.resolver,
        rt.rollback_service,
    )

    print("=" * 72)
    print("Phase 1 | 创建 Prompt v1 / v2（不可变）")
    print("=" * 72)
    await pr.create_prompt(AGENT, created_by="alice")
    await pr.create_version(AGENT, template="请简洁回答问题。", created_by="alice")
    await pr.create_version(AGENT, template="请详细回答问题，并给出三个例子。", created_by="alice")

    print("Phase 2 | 创建 Config v1 / v2（引用 Prompt 版本）")
    await cr.create_config(AGENT, prompt={"name": AGENT, "version": 1}, created_by="alice")
    await cr.create_config(AGENT, prompt={"name": AGENT, "version": 2}, created_by="alice")

    print("Phase 3 | 环境绑定：dev=v1, staging=v1, prod=v1")
    for env in ("dev", "staging", "prod"):
        await pub.publish(AGENT, env, 1, created_by="alice")

    print("Phase 4 | 发布：prod 灰度 v2 @ 10%")
    dep = await pub.publish(AGENT, "prod", 2, traffic_percent=10,
                            experiment="prompt_v2_test", created_by="alice", reason="A/B")
    print(f"   -> {_rules_str(dep)}  [{dep.status}]  experiment={dep.experiment}")

    print("Phase 5 | Runtime Resolve：同一个用户稳定命中同一 Variant")
    for uid in ("user_001", "user_002", "user_003"):
        snap = await rsv.resolve(AGENT, "prod", uid)
        print(f"   -> {uid}: config v{snap.config_version}, "
              f"variant={snap.routing['variant']}, prompt v{snap.prompt['version']}")

    print("Phase 6 | 灰度加流量 10% -> 50% -> 100%（全量发布）")
    for pct in (50, 100):
        dep = await pub.rollout(dep.id, 2, pct, created_by="alice", reason="good metrics")
        print(f"   -> {_rules_str(dep)}  [{dep.status}]")

    print("Phase 7 | 监控告警：Tool Error 5% -> 20%，执行 Rollback")
    print("   （Rollback 不删除 v2 —— 只是路由表回到 v1）")
    dep = await roll.rollback(dep.id, created_by="ops", reason="tool error 5%->20%")
    print(f"   -> {_rules_str(dep)}  [{dep.status}]")
    snap = await rsv.resolve(AGENT, "prod", "user_001")
    print(f"   -> 回滚后 resolve: config v{snap.config_version} (v2 版本仍存在, 只是不路由了)")

    print("Phase 8 | 审计日志（Change Attribution）")
    for e in await rt.audit_service.list(limit=6):
        print(f"   -> [{e.action:<20}] {e.resource_id:<28} by {e.actor:<6} reason={e.reason or '-'}")

    await rt.stop()


if __name__ == "__main__":
    asyncio.run(main())
