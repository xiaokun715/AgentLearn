"""无限内存 → OOM（设计说明书 §21 Test 2）。

内存由 ResourceMonitor 周期性采样 + 越限 kill 保证（§19）。用很小的 memory_mb 上限
和更快的采样间隔，让测试快且确定。
"""
from __future__ import annotations

from app.domain.execution import ExecutionStatus
from app.domain.policy import Policy
from app.security.identity import Identity

MEMORY_BOMB = (
    "x = []\n"
    "while True:\n"
    "    x.append('A' * (1024 * 1024))\n"
)
IDENTITY = Identity(tenant_id="t")


async def _make_tiny_policy(runtime, memory_mb: int = 48) -> None:
    policy = Policy.from_dict({
        "name": "tiny_mem",
        "resources": {"memory_mb": memory_mb, "timeout_seconds": 10, "output_kb": 128},
    })
    await runtime.policy_store.save(policy)


class TestMemory:
    async def test_memory_bomb_oom(self, make_runtime):
        # 更快的采样间隔，确保越限后立刻 kill
        runtime = await make_runtime(monitor_interval=0.05)
        await _make_tiny_policy(runtime, memory_mb=48)

        execution = await runtime.service.create(
            tool_type="python", code=MEMORY_BOMB,
            policy_request={"name": "tiny_mem"}, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=20)

        assert done.status == ExecutionStatus.OOM
        # 越限被杀，不是正常退出
        assert done.exit_code != 0
        # 峰值内存应该记录了越限（≥ 上限）
        assert done.resource_usage.get("memory_peak_mb", 0) >= 48

    async def test_oom_does_not_hang(self, make_runtime):
        runtime = await make_runtime(monitor_interval=0.05)
        await _make_tiny_policy(runtime, memory_mb=64)
        execution = await runtime.service.create(
            tool_type="python", code=MEMORY_BOMB,
            policy_request={"name": "tiny_mem"}, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=20)
        # OOM 或（极慢机器上）被超时兜底 —— 反正不能一直跑
        assert done.status in (ExecutionStatus.OOM, ExecutionStatus.TIMEOUT)
