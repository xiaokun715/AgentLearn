"""Policy Engine / Compiler / Egress 单元测试（设计说明书 §10-13, §17-18）。

核心断言：Agent 可以请求能力，但不能决定最终权限（§10）。
"""
from __future__ import annotations

import pytest

from app.domain.exceptions import InvalidToolType
from app.domain.policy import Policy
from app.network.egress import EgressPolicy
from app.policy.compiler import PolicyCompiler
from app.policy.engine import PolicyEngine
from app.policy.rules import scan_code

BENIGN_CODE = "print(sum(range(100)))"


async def _make_policy(runtime, name: str, *pairs) -> Policy:
    """保存一个带自定义字段的策略（flat key, value, ...），返回对象。"""
    data = {
        "name": name,
        "version": 1,
        "resources": {
            "cpu": 0.5, "memory_mb": 256, "timeout_seconds": 10,
            "pids": 64, "disk_mb": 100, "output_kb": 512,
        },
        "filesystem": {"read": ["/workspace/input"], "write": ["/workspace/output"]},
        "network": {"enabled": False},
    }
    iterator = iter(pairs)
    for key, value in zip(iterator, iterator):
        _set_nested(data, key, value)
    policy = Policy.from_dict(data)
    await runtime.policy_store.save(policy)
    return policy


def _set_nested(data: dict, dotted: str, value):
    keys = dotted.split(".")
    node = data
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


# ------------------------------------------------------------------ rules
class TestStaticRules:
    def test_scan_flags_network_call(self):
        result = scan_code('import requests\nrequests.get("https://example.com")')
        assert "network" in result.findings

    def test_scan_flags_host_filesystem(self):
        result = scan_code('open("/host-secret")')
        assert "filesystem_host" in result.findings

    def test_scan_flags_syscall_escape(self):
        assert "syscall_escape" in scan_code("ctypes.CDLL(None)").findings

    def test_scan_allows_benign(self):
        assert scan_code(BENIGN_CODE).allowed


# ------------------------------------------------------------------ engine
class TestPolicyEngine:
    async def test_allows_within_policy(self, runtime):
        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python", code=BENIGN_CODE,
            policy_name="python_basic", requested=None,
        )
        assert decision.allowed
        assert decision.policy is not None

    async def test_denies_excessive_memory(self, runtime):
        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python", code=BENIGN_CODE,
            policy_name="python_basic",
            requested={"memory_mb": 10_000},  # 远超 256MB 上限
        )
        assert not decision.allowed
        assert "exceeds policy cap" in decision.reason

    async def test_denies_excessive_timeout(self, runtime):
        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python", code=BENIGN_CODE,
            policy_name="python_basic",
            requested={"timeout_seconds": 3600},  # 远超 10s 上限
        )
        assert not decision.allowed

    async def test_denies_network_request_when_policy_disables(self, runtime):
        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python", code=BENIGN_CODE,
            policy_name="python_basic", requested={"network": True},
        )
        assert not decision.allowed
        assert "denied by policy" in decision.reason

    async def test_static_reject_network_usage(self, runtime):
        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python",
            code='import requests\nrequests.get("https://example.com")',
            policy_name="python_basic", requested=None,
        )
        assert not decision.allowed
        assert "static scan" in decision.reason

    async def test_static_reject_host_fs(self, runtime):
        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python", code='open("/etc/passwd")',
            policy_name="python_basic", requested=None,
        )
        assert not decision.allowed

    async def test_network_allowed_requires_allowlist(self, runtime):
        await _make_policy(runtime, "net_bad", "network.enabled", True)
        engine = PolicyEngine(runtime.policy_store)
        # 网络 enabled 但白名单为空 → 相当于 network=true，禁止（§18）
        decision = await engine.evaluate(
            tool_type="python", code=BENIGN_CODE,
            policy_name="net_bad", requested={"network": True},
        )
        assert not decision.allowed

    async def test_forbidden_egress_target_rejected(self, runtime):
        await _make_policy(
            runtime, "net_meta",
            "network.enabled", True,
            "network.allow_domains", ["169.254.169.254"],  # 云 metadata → 禁止
        )
        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python", code=BENIGN_CODE,
            policy_name="net_meta", requested={"network": True},
        )
        assert not decision.allowed
        assert "forbidden egress" in decision.reason

    async def test_networked_policy_allows_listed_domain(self, runtime):
        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python", code=BENIGN_CODE,
            policy_name="python_networked", requested={"network": True},
        )
        assert decision.allowed

    async def test_filesystem_boundary_enforced(self, runtime):
        await _make_policy(runtime, "bad_fs", "filesystem.read", ["/"])  # 越界
        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python", code=BENIGN_CODE,
            policy_name="bad_fs", requested=None,
        )
        assert not decision.allowed
        assert "outside /workspace" in decision.reason

    async def test_fork_bomb_gives_warning_but_allows(self, runtime):
        engine = PolicyEngine(runtime.policy_store)
        decision = await engine.evaluate(
            tool_type="python",
            code="import subprocess\nwhile True:\n    subprocess.Popen(['x'])",
            policy_name="python_basic", requested=None,
        )
        # 静态只给 warning，真正拦截靠 PID limit（§20）
        assert decision.allowed
        assert any("fork bomb" in w for w in decision.warnings)


# ------------------------------------------------------------------ compiler
class TestPolicyCompiler:
    async def test_compiles_python_basic(self, runtime):
        policy = await runtime.policy_store.get("python_basic")
        config = PolicyCompiler().compile(policy, "python")
        assert config.image == "agent-sandbox-python:latest"
        assert config.network_enabled is False
        assert config.read_only_root is True
        assert config.cap_drop == ["ALL"]
        assert config.no_new_privileges is True
        assert "/workspace/input" in config.read_paths
        assert "/workspace/output" in config.write_paths

    async def test_compiler_rejects_unknown_tool(self, runtime):
        policy = await runtime.policy_store.get("python_basic")
        with pytest.raises(InvalidToolType):
            PolicyCompiler().compile(policy, "ruby")


# ------------------------------------------------------------------ egress
class TestEgress:
    def test_disabled_by_default(self):
        decision = EgressPolicy().decide(False, [])
        assert decision.network_enabled is False

    def test_enabled_without_allowlist_is_denied(self):
        decision = EgressPolicy().decide(True, [])
        assert decision.network_enabled is False

    def test_private_ip_in_allowlist_is_denied(self):
        decision = EgressPolicy().decide(True, ["api.example.com", "10.0.0.1"])
        assert decision.network_enabled is False
        assert "10.0.0.1" in decision.denied_domains

    def test_metadata_ip_is_denied(self):
        assert EgressPolicy()._is_forbidden("169.254.169.254")
        assert EgressPolicy()._is_forbidden("127.0.0.1")

    def test_valid_domain_allowed(self):
        decision = EgressPolicy().decide(True, ["api.example.com"])
        assert decision.network_enabled is True
        assert decision.allow_domains == ["api.example.com"]
