"""网络隔离测试（设计说明书 §17-18 Test 4-5）。

默认 network = disabled：Agent 代码里出现网络调用 → 静态规则 REJECTED。
即使 Policy 允许网络，也必须过 allow-list（私网 / metadata 永远禁止）。
"""
from __future__ import annotations

from app.domain.execution import ExecutionStatus
from app.network.egress import EgressPolicy
from app.security.identity import Identity

IDENTITY = Identity(tenant_id="t")

NETWORK_CALL = (
    "import requests\n"
    "requests.get('https://example.com')\n"
)


class TestNetworkIsolation:
    async def test_network_usage_rejected_when_disabled(self, runtime):
        """Test 4：network=false 时 requests.get → DENY。"""
        execution = await runtime.service.create(
            tool_type="python", code=NETWORK_CALL,
            policy_request=None, identity=IDENTITY,  # 默认 python_basic：network 关
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.REJECTED
        assert "network is denied" in (done.error or "")

    async def test_socket_usage_rejected(self, runtime):
        execution = await runtime.service.create(
            tool_type="python", code="import socket\nsocket.socket()",
            policy_request=None, identity=IDENTITY,
        )
        done = await runtime.service.wait(execution.id, timeout=15)
        assert done.status == ExecutionStatus.REJECTED

    async def test_private_network_never_allowed(self, runtime):
        """Test 5：私网 / metadata 即使出现在 allow-list 也会被 Policy 拒绝。"""
        from app.domain.policy import Policy

        for bad in ("10.0.0.5", "192.168.1.1", "169.254.169.254"):
            policy = Policy.from_dict({
                "name": "net_bad",
                "network": {"enabled": True, "allow_domains": [bad]},
            })
            await runtime.policy_store.save(policy)
            execution = await runtime.service.create(
                tool_type="python", code="print('hi')",
                policy_request={"name": "net_bad", "network": True},
                identity=IDENTITY,
            )
            done = await runtime.service.wait(execution.id, timeout=15)
            assert done.status == ExecutionStatus.REJECTED, f"{bad} 必须被拒"
            assert "forbidden egress" in (done.error or "")


class TestEgressDecision:
    def test_metadata_always_forbidden(self):
        policy = EgressPolicy()
        assert policy._is_forbidden("169.254.169.254")
        assert policy._is_forbidden("127.0.0.1")

    def test_private_ranges_forbidden(self):
        policy = EgressPolicy()
        assert policy._is_forbidden("10.0.0.1")
        assert policy._is_forbidden("172.16.0.1")
        assert policy._is_forbidden("192.168.0.1")

    def test_public_domain_allowed(self):
        decision = EgressPolicy().decide(True, ["api.example.com"])
        assert decision.network_enabled is True

    def test_invalid_hostname_rejected(self):
        assert EgressPolicy()._is_forbidden("not a domain")
        assert EgressPolicy()._is_forbidden("localhost")
