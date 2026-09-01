"""Docker Sandbox（设计说明书 §14 —— V1 主实现）。

把 Policy Compiler 输出的 SandboxConfig 翻译成 Docker 安全参数，每个参数都有安全意义：

    mem_limit="256m"            → 内存限制（§19，超出 → OOMKilled）
    nano_cpus=500_000_000       → CPU 限制（§19）
    pids_limit=64               → PID 限制，防 fork bomb（§20）
    network_disabled=True       → 网络隔离（§17，默认全关）
    read_only=True + /workspace → 文件系统边界（§15，只挂工作区）
    tmpfs /tmp size=100m        → 临时盘上限（§19 disk）
    cap_drop=["ALL"]            → 最小权限，丢光全部 capability
    security_opt=[no-new-privileges] → 禁止提权
    user=sandbox                → 非 root 运行

需要 Docker daemon：SANDBOX_BACKEND=docker（或 auto 自动探测）。
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..domain.exceptions import SandboxBackendUnavailable
from ..policy.compiler import SandboxConfig
from .base import Sandbox

logger = logging.getLogger(__name__)


class DockerSandbox(Sandbox):
    name = "docker"

    def __init__(self) -> None:
        try:
            import docker  # 延迟导入：未装 docker 包时给清晰报错
        except ImportError as exc:
            raise SandboxBackendUnavailable(
                "SANDBOX_BACKEND=docker 需要 pip install agent-tool-sandbox[docker]"
            ) from exc
        try:
            self._client = docker.from_env()
            self._client.ping()
        except Exception as exc:  # noqa: BLE001
            raise SandboxBackendUnavailable(
                f"Docker daemon 不可用: {exc}"
            ) from exc

    async def create(self, config: SandboxConfig, workspace) -> str:
        host_config = {
            "mem_limit": f"{config.memory_mb}m",
            "nano_cpus": int(config.cpu * 1_000_000_000),
            "pids_limit": config.pids,
            "network_disabled": not config.network_enabled,
            "read_only": config.read_only_root,
            "tmpfs": {"/tmp": f"size={config.disk_mb}m"},
            "cap_drop": config.cap_drop,
            "security_opt": ["no-new-privileges:true"] if config.no_new_privileges else None,
            "binds": [f"{workspace.host_root}:{workspace.container_root}"],
        }
        host_config = {k: v for k, v in host_config.items() if v is not None}

        container = await asyncio.to_thread(
            self._client.containers.create,
            image=config.image,
            command=config.command,
            working_dir=config.working_dir,
            user="sandbox",
            host_config=self._client.api.create_host_config(**host_config),
        )
        logger.info("docker container created: %s image=%s", container.id[:12], config.image)
        return container.id

    async def start(self, runtime_id: str) -> None:
        container = self._client.containers.get(runtime_id)
        await asyncio.to_thread(container.start)

    async def wait(self, runtime_id: str, timeout: float | None = None) -> int | None:
        """轮询容器状态直到退出（避免 docker wait 的长连接阻塞 + 无法取消）。"""
        container = self._client.containers.get(runtime_id)
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            await asyncio.to_thread(container.reload)
            status = container.status
            if status in ("exited", "dead"):
                state = container.attrs.get("State", {})
                return state.get("ExitCode")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"container did not exit within {timeout}s")
            await asyncio.sleep(0.2)

    async def kill(self, runtime_id: str, reason: str | None = None) -> None:
        """幂等 kill（§23）：容器已停就忽略。"""
        try:
            container = self._client.containers.get(runtime_id)
            await asyncio.to_thread(container.kill)
        except Exception:  # noqa: BLE001
            pass  # 已经退出 / 不存在 → 幂等
        if reason:
            logger.info("docker container %s killed, reason=%s", runtime_id[:12], reason)

    async def collect(self, runtime_id: str) -> tuple[str, str]:
        container = self._client.containers.get(runtime_id)
        out = await asyncio.to_thread(
            container.logs, stdout=True, stderr=False, timestamps=False
        )
        err = await asyncio.to_thread(
            container.logs, stdout=False, stderr=True, timestamps=False
        )
        return (out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

    async def stats(self, runtime_id: str) -> dict:
        try:
            container = self._client.containers.get(runtime_id)
            raw = await asyncio.to_thread(container.stats, stream=False)
        except Exception:  # noqa: BLE001
            return {"cpu_percent": 0.0, "memory_mb": 0.0, "pids": 0}

        cpu_delta = raw.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) - \
            raw.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = raw.get("cpu_stats", {}).get("system_cpu_usage", 0) - \
            raw.get("precpu_stats", {}).get("system_cpu_usage", 0)
        cpu_percent = (cpu_delta / sys_delta * 100.0) if sys_delta else 0.0

        memory_usage = raw.get("memory_stats", {}).get("usage", 0)
        return {
            "cpu_percent": round(cpu_percent, 2),
            "memory_mb": round(memory_usage / (1024 * 1024), 2),
            "pids": raw.get("pids_stats", {}).get("current", 0),
        }

    async def meta(self, runtime_id: str) -> dict:
        try:
            container = self._client.containers.get(runtime_id)
            await asyncio.to_thread(container.reload)
            state = container.attrs.get("State", {})
            return {"oom_killed": bool(state.get("OOMKilled"))}
        except Exception:  # noqa: BLE001
            return {"oom_killed": False}

    async def destroy(self, runtime_id: str) -> None:
        """幂等销毁容器（§25：1 execution = 1 ephemeral runtime）。"""
        try:
            container = self._client.containers.get(runtime_id)
            await asyncio.to_thread(container.remove, force=True)
        except Exception:  # noqa: BLE001
            pass  # 已销毁 → 幂等
