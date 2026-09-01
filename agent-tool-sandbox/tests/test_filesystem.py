"""文件系统边界测试（设计说明书 §15-16 Test 3 + §25 临时 Runtime）。

原则：Default Deny —— Agent 只能碰 /workspace，宿主文件访问必须被拒。
"""
from __future__ import annotations

from app.domain.execution import ExecutionStatus
from app.filesystem.boundary import Workspace
from app.security.identity import Identity

IDENTITY = Identity(tenant_id="t")


class TestFilesystemBoundary:
    async def test_host_secret_denied(self, runtime):
        """Test 3：open('/host-secret') → 静态规则拦截 → REJECTED。"""
        execution = await runtime.service.create(
            tool_type="python", code='open("/host-secret")',
            policy_request=None, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.REJECTED
        assert "host filesystem" in (done.error or "")

    async def test_etc_passwd_denied(self, runtime):
        execution = await runtime.service.create(
            tool_type="python", code='open("/etc/passwd")',
            policy_request=None, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.REJECTED

    async def test_out_of_workspace_policy_rejected(self, runtime):
        """Policy 文件系统边界越界 → 策略本身不可编译 → REJECTED（§16 Default Deny）。"""
        from app.domain.policy import Policy

        await runtime.policy_store.save(Policy.from_dict({
            "name": "bad_fs",
            "filesystem": {"read": ["/etc"], "write": ["/workspace/output"]},
        }))
        execution = await runtime.service.create(
            tool_type="python", code="print('hi')",
            policy_request={"name": "bad_fs"}, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.REJECTED
        assert "outside /workspace" in (done.error or "")


class TestWorkspaceIsolation:
    def test_two_executions_isolated(self):
        """§25：1 execution = 1 ephemeral runtime，工作区互不可见。"""
        ws_a = Workspace.create("aaa", "python", "print('a')")
        ws_b = Workspace.create("bbb", "python", "print('b')")
        assert ws_a.host_root != ws_b.host_root
        assert ws_a.host_root.exists() and ws_b.host_root.exists()
        assert ws_a.code_path.exists() and ws_b.code_path.exists()
        assert ws_a.input_dir.exists() and ws_a.output_dir.exists()
        ws_a.cleanup()
        ws_b.cleanup()

    def test_cleanup_removes_workspace(self):
        ws = Workspace.create("ccc", "python", "print('x')")
        root = ws.host_root
        assert root.exists()
        ws.cleanup()
        assert not root.exists()
