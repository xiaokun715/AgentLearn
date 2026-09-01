"""Sandbox 抽象（设计说明书 §28）。

统一接口让上层（Agent / Tool / Manager）完全不关心底层实现：
    Sandbox
    ├── DockerSandbox         ← V1 主实现
    ├── ProcessSandbox        ← 无 Docker 时的本地兜底（学习/开发）
    ├── KubernetesSandbox     ← V2（未来）
    ├── FirecrackerSandbox    ← V3（未来）
    └── WasmSandbox           ← V3（未来）

生命周期（§26）：CREATE → START → RUN → COLLECT → CLEANUP → DESTROY
无论 SUCCESS / FAIL / TIMEOUT / KILL，最后都必须 destroy()。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Sandbox(ABC):
    """所有沙箱后端的统一接口。"""

    name: str = "base"

    @abstractmethod
    async def create(self, config, workspace) -> str:
        """创建 runtime（容器 / 进程），返回 runtime_id。"""

    @abstractmethod
    async def start(self, runtime_id: str) -> None:
        """启动 runtime。"""

    @abstractmethod
    async def wait(self, runtime_id: str, timeout: float | None = None) -> int | None:
        """等待 runtime 退出，返回 exit_code；超时抛 asyncio.TimeoutError。"""

    @abstractmethod
    async def kill(self, runtime_id: str, reason: str | None = None) -> None:
        """强制终止 runtime（必须幂等，§23）。"""

    @abstractmethod
    async def collect(self, runtime_id: str) -> tuple[str, str]:
        """收集 (stdout, stderr)，已截断到 output 上限。"""

    @abstractmethod
    async def stats(self, runtime_id: str) -> dict:
        """采集实时资源用量（cpu_percent / memory_mb / pids）。"""

    @abstractmethod
    async def meta(self, runtime_id: str) -> dict:
        """读取终止原因等运行时元信息（oom / pid / output_limit_exceeded ...）。"""

    @abstractmethod
    async def destroy(self, runtime_id: str) -> None:
        """销毁 runtime，回收资源（必须幂等）。"""
