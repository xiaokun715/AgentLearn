"""Policy Compiler（设计说明书 §12-13）。

职责：
    High-level Policy → Validation → Normalize → Compile → SandboxConfig

业务层的 `memory_mb: 256`、`network: false` 并不是 Docker/Kubernetes 能直接理解的安全
策略；Compiler 把它们翻译成底层 Runtime 能消费的配置（这里就是 SandboxConfig，Docker
Sandbox 再把它映射成 HostConfig / SecurityContext 等）。

> Policy = 声明式安全策略
> Compiler = 把策略翻译成底层 Runtime 配置
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..domain.exceptions import InvalidToolType
from ..domain.policy import Policy
from ..network.egress import EgressPolicy

logger = logging.getLogger(__name__)

# 每种工具类型 → 沙箱镜像（sandbox_images/ 下的 Dockerfile）与容器内启动命令
TOOL_IMAGES: dict[str, str] = {
    "python": "agent-sandbox-python:latest",
    "shell": "agent-sandbox-python:latest",  # shell 复用 python 镜像（自带 sh）
    "node": "agent-sandbox-node:latest",
    "sql": "agent-sandbox-sql:latest",
}

TOOL_COMMANDS: dict[str, list[str]] = {
    "python": ["python", "/workspace/main.py"],
    "shell": ["/bin/sh", "/workspace/main.sh"],
    "node": ["node", "/workspace/main.js"],
    "sql": ["psql", "-f", "/workspace/main.sql"],
}

SUPPORTED_TOOL_TYPES = tuple(TOOL_IMAGES.keys())


@dataclass(slots=True)
class SandboxConfig:
    """编译产物 —— 沙箱 Runtime 真正消费的配置。

    对应 §14 里 docker run 的每一个安全参数：
        cpu/memory/pids/timeout/disk/output → 资源限制
        read_only_root + 只挂载 /workspace   → 文件系统边界
        network_enabled                      → 网络隔离
        cap_drop + no_new_privileges         → 最小权限
    """

    tool_type: str
    image: str
    command: list[str]
    cpu: float = 0.5
    memory_mb: int = 256
    timeout_seconds: int = 10
    pids: int = 64
    disk_mb: int = 100
    output_kb: int = 512
    network_enabled: bool = False
    allow_domains: list[str] = field(default_factory=list)
    read_paths: list[str] = field(default_factory=list)
    write_paths: list[str] = field(default_factory=list)
    read_only_root: bool = True
    cap_drop: list[str] = field(default_factory=lambda: ["ALL"])
    no_new_privileges: bool = True
    working_dir: str = "/workspace"

    def to_dict(self) -> dict:
        return {
            "tool_type": self.tool_type,
            "image": self.image,
            "command": list(self.command),
            "resources": {
                "cpu": self.cpu,
                "memory_mb": self.memory_mb,
                "timeout_seconds": self.timeout_seconds,
                "pids": self.pids,
                "disk_mb": self.disk_mb,
                "output_kb": self.output_kb,
            },
            "network": {
                "enabled": self.network_enabled,
                "allow_domains": list(self.allow_domains),
            },
            "filesystem": {
                "read": list(self.read_paths),
                "write": list(self.write_paths),
            },
            "read_only_root": self.read_only_root,
            "cap_drop": list(self.cap_drop),
            "no_new_privileges": self.no_new_privileges,
            "working_dir": self.working_dir,
        }


class PolicyCompiler:
    """把 Policy 编译成 SandboxConfig。

    文件系统路径在此归一化：Policy 里写 `/workspace/input`，Docker 挂载时只挂 `/workspace`，
    所以 read_paths/write_paths 保留容器内路径，由 filesystem/boundary.py 负责宿主机侧映射。
    """

    def __init__(self) -> None:
        self.egress = EgressPolicy()

    def compile(self, policy: Policy, tool_type: str) -> SandboxConfig:
        if tool_type not in TOOL_IMAGES:
            raise InvalidToolType(
                f"unsupported tool type: {tool_type} (supported: {SUPPORTED_TOOL_TYPES})"
            )

        # 网络归一化：Policy 里 enable=true 但白名单为空/非法 → 强制关闭（§18）
        egress = self.egress.decide(policy.network.enabled, policy.network.allow_domains)
        network_enabled = egress.network_enabled

        return SandboxConfig(
            tool_type=tool_type,
            image=TOOL_IMAGES[tool_type],
            command=list(TOOL_COMMANDS[tool_type]),
            cpu=policy.resources.cpu,
            memory_mb=policy.resources.memory_mb,
            timeout_seconds=policy.resources.timeout_seconds,
            pids=policy.resources.pids,
            disk_mb=policy.resources.disk_mb,
            output_kb=policy.resources.output_kb,
            network_enabled=network_enabled,
            allow_domains=list(egress.allow_domains),
            read_paths=list(policy.filesystem.read),
            write_paths=list(policy.filesystem.write),
            read_only_root=True,
            cap_drop=["ALL"],
            no_new_privileges=True,
            working_dir="/workspace",
        )
