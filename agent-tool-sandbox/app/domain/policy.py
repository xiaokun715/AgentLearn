"""Policy 领域对象（设计说明书 §10-13）。

Policy 是「声明式安全策略」：描述 Agent *被允许* 做什么，而不是它 *请求* 做什么。
Policy Compiler 负责把它翻译成底层 Runtime 配置（SandboxConfig）。

核心原则（§10）：Agent 可以请求能力，但不能决定最终权限。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class FilesystemPolicy:
    """文件系统边界（§16 Default Deny）。"""

    read: list[str] = field(default_factory=list)    # 只允许读的路径（容器内）
    write: list[str] = field(default_factory=list)   # 只允许写的路径（容器内）


@dataclass(slots=True)
class NetworkPolicy:
    """网络策略（§17-18）。默认 disabled；启用必须提供 allow_domains。"""

    enabled: bool = False
    allow_domains: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ResourcesPolicy:
    """资源限制（§19）。"""

    cpu: float = 0.5
    memory_mb: int = 256
    timeout_seconds: int = 10
    pids: int = 64          # PID 上限，防 fork bomb（§20）
    disk_mb: int = 100      # /tmp 等临时存储上限
    output_kb: int = 512    # stdout+stderr 上限（§24）


@dataclass(slots=True)
class Policy:
    """服务端声明式策略。由 PolicyStore 持久化（§35），带版本便于审计。"""

    name: str
    version: int = 1
    resources: ResourcesPolicy = field(default_factory=ResourcesPolicy)
    filesystem: FilesystemPolicy = field(default_factory=FilesystemPolicy)
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    syscalls_profile: str = "restricted"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        """把 JSON / YAML 形态的 Policy 反序列化为领域对象。"""
        resources = data.get("resources") or {}
        filesystem = data.get("filesystem") or {}
        network = data.get("network") or {}
        return cls(
            name=str(data["name"]),
            version=int(data.get("version", 1)),
            resources=ResourcesPolicy(
                cpu=float(resources.get("cpu", 0.5)),
                memory_mb=int(resources.get("memory_mb", 256)),
                timeout_seconds=int(resources.get("timeout_seconds", 10)),
                pids=int(resources.get("pids", 64)),
                disk_mb=int(resources.get("disk_mb", 100)),
                output_kb=int(resources.get("output_kb", 512)),
            ),
            filesystem=FilesystemPolicy(
                read=list(filesystem.get("read", [])),
                write=list(filesystem.get("write", [])),
            ),
            network=NetworkPolicy(
                enabled=bool(network.get("enabled", False)),
                allow_domains=list(network.get("allow_domains", [])),
            ),
            syscalls_profile=str((data.get("syscalls") or {}).get("profile", "restricted")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "resources": {
                "cpu": self.resources.cpu,
                "memory_mb": self.resources.memory_mb,
                "timeout_seconds": self.resources.timeout_seconds,
                "pids": self.resources.pids,
                "disk_mb": self.resources.disk_mb,
                "output_kb": self.resources.output_kb,
            },
            "filesystem": {
                "read": list(self.filesystem.read),
                "write": list(self.filesystem.write),
            },
            "network": {
                "enabled": self.network.enabled,
                "allow_domains": list(self.network.allow_domains),
            },
            "syscalls": {"profile": self.syscalls_profile},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
