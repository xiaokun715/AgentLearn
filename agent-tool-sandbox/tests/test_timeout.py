"""无限循环 → TIMEOUT（设计说明书 §21 Test 1）。

关键断言：超时后 runtime 必须被真正杀掉，而不是只停止等待。
"""
from __future__ import annotations

import psutil

from app.domain.execution import ExecutionStatus
from app.domain.policy import Policy
from app.security.identity import Identity

INFINITE_LOOP = "while True:\n    pass\n"
IDENTITY = Identity(tenant_id="t")


async def _make_fast_policy(runtime, timeout_seconds: int = 1) -> None:
    policy = Policy.from_dict({
        "name": "fast",
        "resources": {"timeout_seconds": timeout_seconds, "output_kb": 128},
    })
    await runtime.policy_store.save(policy)


class TestTimeout:
    async def test_infinite_loop_times_out(self, runtime):
        await _make_fast_policy(runtime, timeout_seconds=1)
        execution = await runtime.service.create(
            tool_type="python", code=INFINITE_LOOP,
            policy_request={"name": "fast"}, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.TIMEOUT
        assert done.duration_ms is not None and done.duration_ms >= 900
        # 超时后进程必须已经真的被终止（§21）
        assert done.exit_code is not None

    async def test_timeout_does_not_leak_process(self, runtime):
        """§21：API 返回 timeout 时，进程不能还在跑（否则资源泄漏）。"""
        await _make_fast_policy(runtime, timeout_seconds=1)
        execution = await runtime.service.create(
            tool_type="python", code=INFINITE_LOOP,
            policy_request={"name": "fast"}, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.TIMEOUT
        assert execution.runtime_id is not None

        try:
            proc = psutil.Process(int(execution.runtime_id))
        except psutil.NoSuchProcess:
            proc = None
        assert proc is None or not proc.is_running()

    async def test_short_code_not_timeout(self, runtime):
        await _make_fast_policy(runtime, timeout_seconds=5)
        execution = await runtime.service.create(
            tool_type="python", code="print('quick')",
            policy_request={"name": "fast"}, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.SUCCEEDED
