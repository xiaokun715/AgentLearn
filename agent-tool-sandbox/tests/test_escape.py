"""逃逸攻击测试（设计说明书 §29-32 之 Test 6 + §3 核心原则）。

验证「Never trust model-generated code」：命令执行 / 动态加载 / fork bomb 都要被拦下。
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.domain.execution import ExecutionStatus
from app.runtime.resource import ResourceMonitor
from app.security.identity import Identity

IDENTITY = Identity(tenant_id="t")


class TestSyscallEscape:
    async def test_os_system_rejected(self, runtime):
        """直接 shell 调用 → REJECTED。"""
        execution = await runtime.service.create(
            tool_type="python", code='os.system("whoami")',
            policy_request=None, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.REJECTED

    async def test_ctypes_rejected(self, runtime):
        """动态加载 C 库 → REJECTED。"""
        execution = await runtime.service.create(
            tool_type="python", code="import ctypes\nctypes.CDLL(None)",
            policy_request=None, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.REJECTED
        assert "syscall escape" in (done.error or "")

    async def test_os_fork_rejected(self, runtime):
        execution = await runtime.service.create(
            tool_type="python", code="import os\nos.fork()",
            policy_request=None, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.REJECTED


class TestForkBomb:
    async def test_fork_bomb_flagged_at_static(self, runtime):
        """静态层给出 fork bomb warning；真正拦截靠 PID limit（§20）。"""
        from app.policy.engine import PolicyEngine

        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python",
            code="import subprocess\nwhile True:\n    subprocess.Popen(['sleep'])",
            policy_name="python_basic", requested=None,
        )
        assert decision.allowed
        assert any("fork bomb" in w for w in decision.warnings)

    async def test_resource_monitor_blocks_pid_breach(self):
        """PID limit 机制（§20）：子进程数越限 → 触发 kill。"""
        class FakeSandbox:
            killed: str | None = None

            async def stats(self, runtime_id):
                return {"cpu_percent": 1.0, "memory_mb": 10.0, "pids": 100}

            async def kill(self, runtime_id, reason=None):
                self.killed = reason

        sandbox = FakeSandbox()
        stop = asyncio.Event()
        breaches: list[str] = []

        async def on_breach(reason, message):
            breaches.append(reason)

        usage = await ResourceMonitor(interval=0.01).run(
            "r1", sandbox, memory_mb=256, pids=10,
            on_breach=on_breach, stop=stop,
        )
        assert breaches == ["pid"]
        assert usage["pids_peak"] == 100

    @pytest.mark.skipif(os.name == "nt", reason="真实 fork bomb 进程测试在 Linux/Docker 上跑")
    async def test_real_fork_bomb_pid_limit(self, make_runtime):
        """真实 fork bomb：PID 越限被拦（Linux 验证路径，§29 Test 6）。"""
        from app.domain.policy import Policy

        runtime = await make_runtime(monitor_interval=0.05)
        await runtime.policy_store.save(Policy.from_dict({
            "name": "tight",
            "resources": {"pids": 8, "timeout_seconds": 10},
        }))
        code = (
            "import subprocess\n"
            "while True:\n"
            "    subprocess.Popen(['sleep', '30'])\n"
        )
        execution = await runtime.service.create(
            tool_type="python", code=code,
            policy_request={"name": "tight"}, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=20)
        assert done.status in (ExecutionStatus.FAILED, ExecutionStatus.TIMEOUT)


class TestOutputBomb:
    async def test_output_explosion_killed(self, make_runtime):
        """§32 Test 8：print 爆炸 → 输出超限被截断 + kill。"""
        from app.domain.policy import Policy

        runtime = await make_runtime()
        await runtime.policy_store.save(Policy.from_dict({
            "name": "small_out",
            "resources": {"output_kb": 8, "timeout_seconds": 15},
        }))
        execution = await runtime.service.create(
            tool_type="python",
            code="print('A' * 1024 * 1024)",
            policy_request={"name": "small_out"}, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=20)
        assert done.status == ExecutionStatus.OUTPUT_LIMIT_EXCEEDED
        # 输出被截断到上限附近
        assert len(done.stdout) <= 8 * 1024 + 200
