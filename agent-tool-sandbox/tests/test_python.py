"""正常代码执行测试（设计说明书 §2 Demo + §7 API）。

Pipeline：POST /executions → Policy → Sandbox → stdout/stderr → Agent。
"""
from __future__ import annotations

import asyncio
import math

import pytest

from app.domain.execution import ExecutionStatus
from app.security.identity import Identity

IDENTITY = Identity(tenant_id="tenant_a", user_id="u1", agent_id="agent_001")


async def _run_and_wait(runtime, code, policy=None, tool_type="python", identity=IDENTITY):
    execution = await runtime.service.create(
        tool_type=tool_type,
        code=code,
        policy_request=policy,
        identity=identity,
    )
    return await runtime.service.wait(execution.id, timeout=30)


class TestPythonExecution:
    async def test_sum_range(self, runtime):
        result = await _run_and_wait(runtime, "print(sum(range(100)))")
        assert result.status == ExecutionStatus.SUCCEEDED
        assert result.stdout == "4950\n"
        assert result.exit_code == 0

    async def test_math_demo(self, runtime):
        """设计说明书 §2 的 Demo 代码。"""
        code = (
            "import math\n"
            "result = sum(math.sqrt(i) for i in range(100))\n"
            "print(result)\n"
        )
        result = await _run_and_wait(runtime, code)
        assert result.status == ExecutionStatus.SUCCEEDED
        expected = sum(math.sqrt(i) for i in range(100))
        assert float(result.stdout.strip()) == pytest.approx(expected, abs=1e-6)

    async def test_stderr_and_exit_code(self, runtime):
        code = "import sys\nprint('to stdout')\nprint('to stderr', file=sys.stderr)\nsys.exit(3)"
        result = await _run_and_wait(runtime, code)
        assert result.status == ExecutionStatus.FAILED
        assert result.exit_code == 3
        assert "to stdout" in result.stdout
        assert "to stderr" in result.stderr

    async def test_resource_usage_reported(self, runtime):
        result = await _run_and_wait(runtime, "print('hi')")
        assert result.status == ExecutionStatus.SUCCEEDED
        assert result.duration_ms is not None
        assert result.resource_usage.get("memory_peak_mb", 0) > 0


class TestShellExecution:
    async def test_echo(self, runtime):
        result = await _run_and_wait(runtime, "echo hello-from-shell", tool_type="shell",
                                     policy={"name": "shell_basic"})
        assert result.status == ExecutionStatus.SUCCEEDED
        assert "hello-from-shell" in result.stdout

    async def test_unsupported_tool_rejected_by_api(self, client):
        resp = await client.post("/v1/executions", json={"type": "ruby", "code": "puts 1"})
        assert resp.status_code == 422


class TestApiSmoke:
    async def test_create_then_get(self, client):
        resp = await client.post(
            "/v1/executions",
            headers={"x-tenant-id": "tenant_b", "x-agent-id": "ag"},
            json={"type": "python", "code": "print(2 + 2)"},
        )
        assert resp.status_code == 201
        body = resp.json()
        execution_id = body["execution_id"]
        assert body["status"] == "queued"

        # 轮询直到终态
        for _ in range(50):
            view = (await client.get(f"/v1/executions/{execution_id}")).json()
            if view["status"] in ("succeeded", "failed", "rejected", "timeout", "oom", "killed"):
                break
            await asyncio.sleep(0.1)
        assert view["status"] == "succeeded"
        assert view["stdout"] == "4\n"

    async def test_list_executions(self, client):
        await client.post("/v1/executions", json={"type": "python", "code": "print(1)"})
        resp = await client.get("/v1/executions")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_list_policies(self, client):
        resp = await client.get("/v1/policies")
        assert resp.status_code == 200
        names = {p["name"] for p in resp.json()}
        assert "python_basic" in names
