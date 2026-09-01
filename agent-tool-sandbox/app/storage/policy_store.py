"""PolicyStore 接口（设计说明书 §35 sandbox_policies 表）+ 内置默认策略。

Policy 必须持久化并带版本 —— 这样某次执行出了问题，能精确回溯到
「Execution → Policy v2 → Sandbox Config」，而不是「现在的 Policy」。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..domain.policy import Policy

# ---------------------------------------------------------------------------
# 内置默认策略（§11 示例）：Policy 是声明式安全策略。
# 注意 network: enabled: false —— 默认全隔离（§17）。
# ---------------------------------------------------------------------------
DEFAULT_POLICIES: dict[str, dict] = {
    "python_basic": {
        "name": "python_basic",
        "version": 1,
        "resources": {
            "cpu": 0.5,
            "memory_mb": 256,
            "timeout_seconds": 10,
            "pids": 64,
            "disk_mb": 100,
            "output_kb": 512,
        },
        "filesystem": {
            "read": ["/workspace/input"],
            "write": ["/workspace/output"],
        },
        "network": {"enabled": False},
        "syscalls": {"profile": "restricted"},
    },
    "shell_basic": {
        "name": "shell_basic",
        "version": 1,
        "resources": {
            "cpu": 0.5,
            "memory_mb": 128,
            "timeout_seconds": 5,
            "pids": 32,
            "disk_mb": 50,
            "output_kb": 256,
        },
        "filesystem": {"read": [], "write": ["/workspace/output"]},
        "network": {"enabled": False},
        "syscalls": {"profile": "restricted"},
    },
    "sql_readonly": {
        "name": "sql_readonly",
        "version": 1,
        "resources": {
            "cpu": 0.5,
            "memory_mb": 256,
            "timeout_seconds": 15,
            "pids": 16,
            "disk_mb": 100,
            "output_kb": 512,
        },
        "filesystem": {"read": ["/workspace/input"], "write": []},
        "network": {"enabled": False},
        "syscalls": {"profile": "restricted"},
    },
    # 演示 egress allow-list（§18）：想上网必须显式列白名单，绝不能 network=true
    "python_networked": {
        "name": "python_networked",
        "version": 1,
        "resources": {
            "cpu": 0.5,
            "memory_mb": 256,
            "timeout_seconds": 15,
            "pids": 64,
            "disk_mb": 100,
            "output_kb": 512,
        },
        "filesystem": {"read": [], "write": ["/workspace/output"]},
        "network": {"enabled": True, "allow_domains": ["api.example.com"]},
        "syscalls": {"profile": "restricted"},
    },
}


def default_policy_objects() -> list[Policy]:
    return [Policy.from_dict(data) for data in DEFAULT_POLICIES.values()]


class PolicyStore(ABC):
    """Policy 的读写接口。"""

    @abstractmethod
    async def save(self, policy: Policy) -> None:
        """保存（带版本）。"""

    @abstractmethod
    async def get(self, name: str) -> Policy | None:
        """按名字读取最新版本。"""

    @abstractmethod
    async def get_default(self) -> Policy | None:
        """读取默认策略（用于未指定 policy_name 的执行）。"""

    @abstractmethod
    async def list(self) -> list[Policy]:
        """列出全部策略（去重，取最新版本）。"""
