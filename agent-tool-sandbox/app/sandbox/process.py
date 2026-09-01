"""Process Sandbox —— 无 Docker 时的本地兜底（学习 / 开发用，设计说明书 §14 的降级实现）。

用 subprocess + psutil 实现简易资源治理：
    - timeout：asyncio.wait_for + 进程树 kill
    - memory：psutil 轮询，超限 kill（→ OOM）
    - pid：监控子进程数，超限 kill（→ 防 fork bomb）
    - output：有界管道采集，超限立刻 kill（→ OUTPUT_LIMIT_EXCEEDED）
    - 网络：清理代理环境变量；真正的隔离需要 namespace / 容器，这里只做提醒

⚠️ 这只是本地 Demo 兜底，隔离强度远弱于 Docker。生产必须用 Docker / gVisor / Firecracker。
它存在的意义是：Sandbox 抽象不依赖 Docker 也能跑，让架构可以随时换后端。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
import time

import psutil

from ..domain.exceptions import SandboxBackendUnavailable
from ..policy.compiler import SandboxConfig
from .base import Sandbox

logger = logging.getLogger(__name__)


class ProcessSandbox(Sandbox):
    name = "process"

    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._output: dict[str, dict[str, bytes]] = {}
        self._meta: dict[str, dict] = {}

    # ------------------------------------------------------------------ create
    async def create(self, config: SandboxConfig, workspace) -> str:
        interpreter = self._resolve_interpreter(config.tool_type)
        cmd = [*interpreter, str(workspace.code_path)]
        proc = subprocess.Popen(
            cmd,
            cwd=str(workspace.host_root),
            env=self._minimal_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        runtime_id = str(proc.pid)
        self._procs[runtime_id] = proc
        self._output[runtime_id] = {}
        self._meta[runtime_id] = {}

        # 有界输出采集（§24 output limit）：线程里边读边截断，超限立刻杀进程
        limit = config.output_kb * 1024
        self._start_drain(runtime_id, proc.stdout, "stdout", limit)
        self._start_drain(runtime_id, proc.stderr, "stderr", limit)

        logger.info("process spawned: pid=%s tool=%s output_limit=%d", runtime_id, config.tool_type, limit)
        return runtime_id

    def _resolve_interpreter(self, tool_type: str) -> list[str]:
        if tool_type == "python":
            return [os.sys.executable]
        if tool_type == "node":
            node = shutil.which("node")
            if not node:
                raise SandboxBackendUnavailable("node 不在 PATH 中")
            return [node]
        if tool_type == "shell":
            for name in ("sh", "bash"):
                shell = shutil.which(name)
                if shell:
                    return [shell]
            raise SandboxBackendUnavailable("sh/bash 不在 PATH 中")
        raise SandboxBackendUnavailable(
            f"tool type '{tool_type}' 只在 Docker 沙箱中支持（ProcessSandbox 支持 python/node/shell）"
        )

    @staticmethod
    def _minimal_env() -> dict:
        """最小化环境：去掉代理，避免 Agent 代码借用宿主网络出口。"""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        for key in ("SystemRoot", "TEMP", "TMP", "ComSpec", "HOME",
                    "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            if key in os.environ:
                env[key] = os.environ[key]
        for key in list(os.environ):
            if key.upper().startswith(("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")):
                env[key] = ""
        return env

    def _start_drain(self, runtime_id: str, stream, name: str, limit: int) -> None:
        def _drain() -> None:
            buf = bytearray()
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    if len(buf) + len(chunk) > limit:
                        keep = limit - len(buf)
                        if keep > 0:
                            buf += chunk[:keep]
                        self._output.setdefault(runtime_id, {})[name] = bytes(buf)
                        # 进程还在疯狂写输出 → 立刻杀掉，避免等满整个 timeout
                        self._meta.setdefault(runtime_id, {})["termination_reason"] = "output_limit_exceeded"
                        self._kill_tree(runtime_id)
                        return
                    buf += chunk
            finally:
                # setdefault：destroy() 可能已经清掉了 runtime_id，避免 KeyError
                self._output.setdefault(runtime_id, {})[name] = bytes(buf)

        threading.Thread(target=_drain, daemon=True,
                         name=f"drain-{runtime_id}-{name}").start()

    # ------------------------------------------------------------- lifecycle
    async def start(self, runtime_id: str) -> None:
        pass  # Popen 已启动

    async def wait(self, runtime_id: str, timeout: float | None = None) -> int | None:
        proc = self._procs[runtime_id]
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            code = proc.poll()
            if code is not None:
                return code
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"process {runtime_id} did not exit within {timeout}s")
            await asyncio.sleep(0.05)

    async def kill(self, runtime_id: str, reason: str | None = None) -> None:
        self._kill_tree(runtime_id, reason)

    def _kill_tree(self, runtime_id: str, reason: str | None = None) -> None:
        """杀掉进程树（幂等）。reason 记录到 meta，供 executor 判定最终状态。"""
        if reason:
            self._meta.setdefault(runtime_id, {})["termination_reason"] = reason
        proc = self._procs.get(runtime_id)
        if proc is None or proc.poll() is not None:
            return  # 已退出 → 幂等
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
        except psutil.NoSuchProcess:
            pass

    async def collect(self, runtime_id: str) -> tuple[str, str]:
        out = self._output.get(runtime_id, {}).get("stdout", b"")
        err = self._output.get(runtime_id, {}).get("stderr", b"")
        # Windows 管道会把 \n 转成 \r\n，统一归一化成 \n，保证跨平台断言一致
        return (
            out.decode("utf-8", "replace").replace("\r\n", "\n"),
            err.decode("utf-8", "replace").replace("\r\n", "\n"),
        )

    async def stats(self, runtime_id: str) -> dict:
        """采集进程树用量。进程可能刚好被杀（NoSuchProcess），一律兜底返回 0。"""
        try:
            parent = psutil.Process(int(runtime_id))
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            return {"cpu_percent": 0.0, "memory_mb": 0.0, "pids": 0}
        try:
            children = parent.children(recursive=True)
            mem = parent.memory_info().rss
            for child in children:
                try:
                    mem += child.memory_info().rss
                except psutil.NoSuchProcess:
                    pass
            return {
                "cpu_percent": round(parent.cpu_percent(interval=None), 2),
                "memory_mb": round(mem / (1024 * 1024), 2),
                "pids": 1 + len(children),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return {"cpu_percent": 0.0, "memory_mb": 0.0, "pids": 0}

    async def meta(self, runtime_id: str) -> dict:
        return dict(self._meta.get(runtime_id, {}))

    async def destroy(self, runtime_id: str) -> None:
        self._kill_tree(runtime_id)
        self._procs.pop(runtime_id, None)
        self._output.pop(runtime_id, None)
        self._meta.pop(runtime_id, None)
