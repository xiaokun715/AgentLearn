"""Policy Engine（设计说明书 §10-11 —— 核心）。

输入：
    - Agent 请求（可能想要更多能力：更大的内存、更长的超时、网络...）
    - 服务端 Policy（持久化的声明式安全策略）

输出：ALLOW / DENY。

核心原则：
> Agent 可以请求能力，但不能决定最终权限。

请求的资源一旦超过 Policy 上限 → DENY（而不是悄悄 clamp），保证可审计。
静态规则扫描是第一层防线（rules.py）；真正的隔离由沙箱 Runtime 保证。

Policy Engine 回答的正是面试题那句：
> Authorization（能不能做？）—— Policy 决定
> Isolation（做了也不能影响谁？）—— Sandbox 决定
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..domain.exceptions import PolicyNotFound
from ..domain.policy import Policy
from ..network.egress import EgressPolicy
from ..storage.policy_store import PolicyStore
from .rules import scan_code

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    policy: Policy | None = None
    warnings: list[str] = field(default_factory=list)


class PolicyEngine:
    """校验 Agent 请求 vs 服务端 Policy，产出 ALLOW / DENY 决策。"""

    def __init__(self, policy_store: PolicyStore) -> None:
        self.policy_store = policy_store
        self.egress = EgressPolicy()

    async def evaluate(
        self,
        *,
        tool_type: str,
        code: str,
        policy_name: str | None,
        requested: dict | None,
    ) -> PolicyDecision:
        policy = await self._load_policy(policy_name)
        req = requested or {}
        warnings: list[str] = []

        # ---- 1) 静态规则第一层扫描（§3 Never trust model-generated code） --------
        static = scan_code(code)
        if "network" in static.findings and not policy.network.enabled:
            return PolicyDecision(
                allowed=False,
                reason=f"static scan: {static.findings['network'][0]} but network is denied",
                policy=policy,
            )
        if "filesystem_host" in static.findings:
            return PolicyDecision(
                allowed=False,
                reason=f"static scan: {static.findings['filesystem_host'][0]} (host filesystem denied)",
                policy=policy,
            )
        if "syscall_escape" in static.findings:
            return PolicyDecision(
                allowed=False,
                reason=f"static scan: {static.findings['syscall_escape'][0]} (syscall escape denied)",
                policy=policy,
            )
        if "fork_bomb" in static.findings:
            warnings.append(
                f"static scan flagged possible fork bomb: {static.findings['fork_bomb'][0]}"
            )

        # ---- 2) 资源请求不能超过 Policy 上限（§10） --------------------------------
        caps = policy.resources
        if req.get("timeout_seconds") is not None and req["timeout_seconds"] > caps.timeout_seconds:
            return PolicyDecision(
                allowed=False,
                reason=f"requested timeout {req['timeout_seconds']}s exceeds policy cap {caps.timeout_seconds}s",
                policy=policy,
            )
        if req.get("memory_mb") is not None and req["memory_mb"] > caps.memory_mb:
            return PolicyDecision(
                allowed=False,
                reason=f"requested memory {req['memory_mb']}MB exceeds policy cap {caps.memory_mb}MB",
                policy=policy,
            )
        if req.get("cpu") is not None and req["cpu"] > caps.cpu:
            return PolicyDecision(
                allowed=False,
                reason=f"requested cpu {req['cpu']} exceeds policy cap {caps.cpu}",
                policy=policy,
            )

        # ---- 3) 网络能力受 Policy 约束（§17-18） ----------------------------------
        if req.get("network") is True and not policy.network.enabled:
            return PolicyDecision(
                allowed=False,
                reason="network requested but denied by policy",
                policy=policy,
            )

        # ---- 4) Egress Allow-List 校验（即使 Policy 允许网络，也要过白名单） ---------
        egress = self.egress.decide(policy.network.enabled, policy.network.allow_domains)
        if egress.denied_domains:
            return PolicyDecision(
                allowed=False,
                reason=f"forbidden egress target(s) in allow list: {egress.denied_domains}",
                policy=policy,
            )
        if not egress.network_enabled and policy.network.enabled:
            # 网络 enabled 但没有合法 allow_domains：等于 network=true，禁止（§18）
            return PolicyDecision(allowed=False, reason=egress.reason, policy=policy)

        # ---- 5) 文件系统边界必须全部在 /workspace 内（Default Deny，§15-16） --------
        for path in policy.filesystem.read + policy.filesystem.write:
            if not (path == "/workspace" or path.startswith("/workspace/")):
                return PolicyDecision(
                    allowed=False,
                    reason=f"filesystem boundary violation: '{path}' is outside /workspace",
                    policy=policy,
                )

        return PolicyDecision(allowed=True, policy=policy, warnings=warnings)

    async def _load_policy(self, policy_name: str | None) -> Policy:
        if policy_name:
            policy = await self.policy_store.get(policy_name)
        else:
            policy = await self.policy_store.get_default()
        if policy is None:
            raise PolicyNotFound(f"policy not found: {policy_name}")
        return policy
