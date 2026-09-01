"""Kill Switch 测试（设计说明书 §22-23）。

流程：POST /kill → Execution Manager → Sandbox Runtime → 真正终止 → KILLED。
要求幂等：重复 kill 不报 500，返回同一 killed 状态。
"""
from __future__ import annotations

import asyncio
import time

from app.domain.execution import ExecutionStatus
from app.domain.policy import Policy
from app.security.identity import Identity

IDENTITY = Identity(tenant_id="t")
LONG_SLEEP = "import time\ntime.sleep(60)\n"


async def _make_long_policy(runtime, timeout_seconds: int = 60) -> None:
    await runtime.policy_store.save(Policy.from_dict({
        "name": "long",
        "resources": {"timeout_seconds": timeout_seconds, "output_kb": 128},
    }))


async def _wait_until_running(service, execution_id, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        execution = await service.get(execution_id)
        if execution.runtime_id is not None:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("execution never started (runtime_id None)")


class TestKillSwitch:
    async def test_kill_terminates_running_execution(self, runtime):
        await _make_long_policy(runtime)
        execution = await runtime.service.create(
            tool_type="python", code=LONG_SLEEP,
            policy_request={"name": "long"}, identity=IDENTITY,
        )
        await _wait_until_running(runtime.service, execution.id)

        killed = await runtime.service.kill(execution.id)
        done = await runtime.service.wait(execution.id, timeout=15)

        assert done.status == ExecutionStatus.KILLED
        assert killed.status == ExecutionStatus.KILLED
        # 在 60s 超时之前就被 kill 了
        assert done.duration_ms is not None and done.duration_ms < 60000

    async def test_kill_is_idempotent(self, runtime):
        """§23：重复 kill 不能 500，应返回同一 killed 状态。"""
        await _make_long_policy(runtime)
        execution = await runtime.service.create(
            tool_type="python", code=LONG_SLEEP,
            policy_request={"name": "long"}, identity=IDENTITY,
        )
        await _wait_until_running(runtime.service, execution.id)

        await runtime.service.kill(execution.id)
        await runtime.service.wait(execution.id, timeout=15)

        # 第二次 kill —— 必须幂等，不能抛异常 / 500
        again = await runtime.service.kill(execution.id)
        assert again.status == ExecutionStatus.KILLED

    async def test_kill_queued_execution(self, runtime):
        """执行还在排队（runtime 未创建）时 kill → 直接落 KILLED，不启动 runtime。"""
        await _make_long_policy(runtime)
        execution = await runtime.service.create(
            tool_type="python", code=LONG_SLEEP,
            policy_request={"name": "long"}, identity=IDENTITY,
        )
        killed = await runtime.service.kill(execution.id)
        assert killed.status == ExecutionStatus.KILLED
        assert killed.runtime_id is None

    async def test_kill_via_api(self, client):
        # 用默认 python_basic（timeout 10s）；60s 的 sleep 会在超时前被 kill
        resp = await client.post(
            "/v1/executions",
            json={"type": "python", "code": LONG_SLEEP},
        )
        assert resp.status_code == 201
        execution_id = resp.json()["execution_id"]
        # 等它开始跑
        for _ in range(100):
            view = (await client.get(f"/v1/executions/{execution_id}")).json()
            if view["status"] in ("running", "starting"):
                break
            await asyncio.sleep(0.05)

        kill_resp = await client.post(f"/v1/executions/{execution_id}/kill")
        assert kill_resp.status_code == 200
        assert kill_resp.json()["status"] == "killed"

        # 幂等：再次 kill 依然 200，不是 500
        again = await client.post(f"/v1/executions/{execution_id}/kill")
        assert again.status_code == 200
        assert again.json()["status"] == "killed"
