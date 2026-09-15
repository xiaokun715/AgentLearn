"""Docker Sandbox —— Demo 主实现（说明书 §27 / §28）。

把 :class:`~app.domain.policy.SandboxPolicy` 逐字段翻译成 Docker 的安全参数，
每条参数都对应 §27 表里的一行限制：

    ==================== ====================================================
    §27 限制              翻译成的 Docker 参数
    ==================== ====================================================
    CPU                    ``--cpus``（速率上限，不是累计 CPU 秒）
    Memory                 ``--memory`` + ``--memory-swap``（同值 ⇒ 禁用 swap）
    Disk                   ``--storage-opt size=``（overlay2+pquota 才支持，失败降级）
    Process                ``--pids-limit``（防 fork bomb）
    Network                ``--network none`` / ``bridge``
    Execution Time         manager 侧的 ``timeout_seconds`` + 超时 ``docker kill``
    Filesystem             ``-v workspace:/workspace`` + ``--read-only`` + ``--tmpfs /tmp``
    ==================== ====================================================

生命周期用 ``docker create`` 一个**常驻空转容器** + ``docker exec`` 执行命令，
而不是把命令烘焙进 ``create``：协议是 ``create(spec) -> run(runtime_id, argv)``，
argv 在 create 之后才给，一个 runtime 可能要跑多条命令（先测试、再覆盖率）。

只用 ``subprocess`` 调 ``docker`` CLI，**不引入 docker python SDK** ——
沙箱的后端实现多一个第三方依赖，隔离面就多一个供给链风险面。

生产请换 **Kubernetes Pod + Namespace + ResourceQuota + NetworkPolicy +
SecurityContext**（§28）：Pod 级隔离比「共享 daemon 的容器」强，
且 NetworkPolicy 是真断网，而不是仅换网卡模式。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

from ..domain.errors import SandboxError
from ..domain.policy import SandboxPolicy
from ..infra.clock import Clock, SystemClock
from .base import ExecResult, SandboxSpec
from .process import TIMEOUT_EXIT_CODE, _decode

logger = logging.getLogger(__name__)

#: 容器内的工作区挂载点 —— 必须与 SandboxSpec.workspace 的宿主路径分开记，
#: Tool 里写的 ``/workspace/out.txt`` 是容器路径，不能与宿主路径混用。
CONTAINER_WORKSPACE = "/workspace"

#: 探测 daemon 的超时。给太长会让 auto 模式启动变慢，太短会误判为不可用。
_DOCKER_PROBE_TIMEOUT_S = 5.0


class DockerSandbox:
    """容器后端。:attr:`name` 固定为 ``"docker"``。"""

    name = "docker"

    def __init__(self, *, clock: Clock | None = None, image: str = "python:3.10-slim") -> None:
        self._clock: Clock = clock or SystemClock()
        self.image = image
        self._containers: dict[str, _Container] = {}
        # available() 探测结果缓存：它会被 auto 模式和每次 open() 问到，
        # 而每次开子进程跑 `docker info` 要几十到几百毫秒。
        self._available: bool | None = None

    # ------------------------------------------------------------------ 能力
    def available(self) -> bool:
        """``docker`` 二进制在 + daemon 活着 才算可用。结果缓存。

        ``docker info`` 会真的连 daemon（``--version`` 只打印客户端版本，
        守护进程没起也会"成功"，所以不能用它探测）。
        """
        if self._available is not None:
            return self._available

        if shutil.which("docker") is None:
            logger.info("Docker 不可用：PATH 中没有 docker 可执行文件")
            self._available = False
            return False

        try:
            proc = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=_DOCKER_PROBE_TIMEOUT_S,
                check=False,
            )
            self._available = proc.returncode == 0
            if not self._available:
                logger.info("Docker 不可用：docker info 退出码 %s (%s)",
                            proc.returncode, (proc.stderr or "").strip()[:200])
        except (OSError, subprocess.SubprocessError) as exc:
            logger.info("Docker 不可用：%s", exc)
            self._available = False
        return self._available

    # ---------------------------------------------------------------- create
    def create(self, spec: SandboxSpec) -> str:
        """创建一个常驻空转容器并启动它，返回 ``container_id``。

        Docker 不可用时 raise :class:`~app.domain.errors.SandboxError`，
        由 :class:`~app.sandbox.manager.SandboxManager` 决定退化还是失败 ——
        后端自己不做「偷偷降级」这种决定，那会让隔离强度在无人察觉时变弱。
        """
        if not self.available():
            raise SandboxError(
                "Sandbox 后端 docker 不可用（docker 未安装或 daemon 未启动）",
                detail={"backend": self.name},
            )

        image = spec.image or self.image
        args = self._create_args(spec, image)

        code, out, err = self._run_docker(args)
        if code != 0 and "--storage-opt" in args:
            # ``--storage-opt size=`` 只在 overlay2 + xfs pquota 上生效，
            # 其他存储驱动会直接报错。磁盘配额是 §27 里最"软"的一条限制，
            # 为了它把整个沙箱开不起来不值得 —— 去掉它重试一次，并留下告警。
            logger.warning("容器存储配额不被支持，降级为不限制磁盘: %s", err.strip()[:200])
            args = [a for a in args if not a.startswith("size=")]
            args = [a for a in args if a != "--storage-opt"]
            code, out, err = self._run_docker(args)

        if code != 0:
            raise SandboxError(
                f"创建沙箱容器失败: {err.strip()[:400]}",
                detail={"image": image, "backend": self.name},
            )

        container_id = out.strip().splitlines()[-1].strip()
        if not container_id:
            raise SandboxError("docker create 未返回容器 ID", detail={"image": image})

        # 立刻启动：容器要先跑起来，后续 docker exec 才有附着点。
        code, _, err = self._run_docker(["docker", "start", container_id])
        if code != 0:
            self._run_docker(["docker", "rm", "-f", container_id])
            raise SandboxError(
                f"启动沙箱容器失败: {err.strip()[:400]}",
                detail={"container_id": container_id[:12]},
            )

        self._containers[container_id] = _Container(spec=spec, workspace=spec.workspace)
        logger.info(
            "docker sandbox created: %s call_id=%s image=%s network=%s",
            container_id[:12], spec.call_id, image, spec.policy.network,
        )
        return container_id

    def _create_args(self, spec: SandboxSpec, image: str) -> list[str]:
        """把 §27 的策略翻译成 ``docker create`` 参数。单独成方法便于逐条对照。"""
        policy = spec.policy
        args = ["docker", "create", "--rm"]

        # --- §27 CPU / Memory / Disk / Process ---
        # docker 要求 --cpus >= 0.01，0 会被拒绝；这里夹一下而不是让 create 失败。
        args += ["--cpus", str(max(0.01, policy.cpu))]
        args += ["--memory", f"{policy.memory_mb}m"]
        # --memory-swap 不给的话默认是 memory 的两倍，容器可以靠 swap 绕过内存上限。
        args += ["--memory-swap", f"{policy.memory_mb}m"]
        args += ["--pids-limit", str(policy.max_processes)]
        args += ["--storage-opt", f"size={policy.disk_mb}M"]

        # --- §27 Network ---
        # 默认 none：需要联网的 Tool 必须在策略里显式声明，而不是默认放开。
        args += ["--network", "bridge" if policy.network else "none"]

        # --- §27 Filesystem ---
        # 工作区只挂工作区，宿主机其余部分不可见（这是容器相对 process 后端
        # 最本质的差别：不是"能不能写"，而是"看不看得见"）。
        args += ["-v", f"{spec.workspace}:{CONTAINER_WORKSPACE}"]
        args += ["-w", CONTAINER_WORKSPACE]
        # readonly_paths 是 Tool 可读不可改的目录（如测试用例）→ 以 :ro 二次挂载。
        # 容器内的挂载点沿用策略里声明的名字（取末段），保证 Tool 里写的
        # ``/workspace/tests`` 和策略里的 ``/workspace/tests/`` 指的是同一个地方。
        for path in policy.readonly_paths:
            host = self._host_path(path, spec.workspace)
            name = os.path.basename(path.rstrip("/")) or "readonly"
            args += ["-v", f"{host}:{CONTAINER_WORKSPACE}/{name}:ro"]
        if policy.readonly_paths:
            # 既然声明了"有东西只读"，就把根文件系统也锁成只读，
            # 只留 /tmp 这个 tmpfs 给解释器写临时文件。
            # 容器内没有 :ro 之外的写入点，Tool 就改不了镜像里的任何东西。
            args += ["--read-only", "--tmpfs", f"/tmp:size={min(policy.disk_mb, 512)}m,noexec,nosuid"]

        # --- 提权面收窄（不在 §27 表里，但成本为零、收益明确）---
        args += ["--cap-drop", "ALL"]
        args += ["--security-opt", "no-new-privileges"]

        # --- 环境变量 ---
        # 与 process 后端同一条原则：不把宿主的整份 os.environ 灌进沙箱。
        env = {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        env.update(spec.env or {})
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]

        # --- 常驻空转 ---
        # 重写 entrypoint 而不是依赖镜像自带的 CMD：不同镜像的 CMD 各不相同，
        # 显式指定 sleep 才能保证容器一定是"活着但不干活"的状态。
        args += ["--entrypoint", "sleep", image, "infinity"]
        return args

    @staticmethod
    def _host_path(path: str, workspace: str) -> str:
        """把策略里的容器路径翻成宿主路径（``/workspace/tests`` -> ``<ws>/tests``）。

        两侧的 ``/`` 都要剥掉：策略里惯常写成 ``/workspace/tests/``（带尾斜杠），
        直接拼进去会得到 ``<ws>/tests/``，虽然 docker 能容忍，
        但 ``-v`` 里出现尾斜杠会让挂载点在日志/审计里看起来像另一个目录。
        """
        if path.startswith(CONTAINER_WORKSPACE):
            rel = path[len(CONTAINER_WORKSPACE):].strip("/")
            return os.path.join(workspace, rel) if rel else workspace
        return path

    # ------------------------------------------------------------------- run
    def run(
        self,
        runtime_id: str,
        argv: list[str],
        *,
        cwd: str | None = None,
        stdin: str = "",
        timeout_seconds: float | None = None,
    ) -> ExecResult:
        """在容器里 ``docker exec`` 一条命令。"""
        container = self._containers.get(runtime_id)
        if container is None:
            raise SandboxError(
                f"沙箱容器不存在或已销毁: {runtime_id}",
                detail={"runtime_id": runtime_id},
            )
        if container.dead:
            return ExecResult(
                exit_code=-1,
                stderr=f"容器 {runtime_id[:12]} 已被 kill，拒绝再执行",
                killed=True,
                meta={"backend": self.name, "reason": container.kill_reason},
            )

        timeout = self._effective_timeout(container.spec.policy, timeout_seconds)
        workdir = self._to_container_path(cwd, container.workspace) if cwd else CONTAINER_WORKSPACE
        started = self._clock.time()

        args = ["docker", "exec", "-i", "-w", workdir, runtime_id, *argv]
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise SandboxError(
                f"无法调用 docker exec: {exc}", detail={"argv": argv}
            ) from exc

        container.proc = proc
        timed_out = False
        try:
            stdout, stderr = proc.communicate(stdin or None, timeout=timeout)
        except subprocess.TimeoutExpired:
            # 杀 docker CLI 客户端**不足以**停掉容器里的进程（exec 出去的是
            # 容器内的独立进程），必须真的 kill 容器 —— 这就是 §29
            # 「超时后要真正终止运行时，而不是只停止等待」在本后端的落点。
            timed_out = True
            self.kill(runtime_id, reason=f"timeout>{timeout}s")
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                stdout, stderr = "", ""

        duration_ms = int((self._clock.time() - started) * 1000)
        container.proc = None

        # docker exec 会把容器内命令的退出码透传出来，直接用即可。
        exit_code = TIMEOUT_EXIT_CODE if timed_out else (
            proc.returncode if proc.returncode is not None else -1
        )
        return ExecResult(
            exit_code=exit_code,
            stdout=_decode(stdout),
            stderr=_decode(stderr),
            duration_ms=duration_ms,
            timed_out=timed_out,
            killed=False,
            # 容器 OOMKilled 的判定要再查一次 inspect，这里只做已知信息的映射：
            # 退出码 137 = 128+SIGKILL，容器里最常见的成因就是内存超限。
            resource_exhausted=(not timed_out and exit_code == 137),
            meta={
                "backend": self.name,
                "container_id": runtime_id[:12],
                "image": container.spec.image or self.image,
                "effective_timeout_s": timeout,
                "duration_ms": duration_ms,
                "cwd": workdir,
            },
        )

    def _effective_timeout(self, policy: SandboxPolicy, requested: float | None) -> float:
        """同 process 后端：调用方的 timeout 只能收紧，不能突破策略上限。"""
        limit = float(policy.timeout_seconds)
        if requested is None:
            return limit
        return max(0.1, min(float(requested), limit))

    @staticmethod
    def _to_container_path(path: str, workspace: str) -> str:
        """把宿主工作区路径翻成容器内路径，供 ``docker exec -w`` 使用。

        Tool 拿到的是宿主路径（``ctx.workspace``），但容器里看到的是
        ``/workspace`` —— 不翻译的话 ``docker exec -w`` 会直接报
        "no such directory"，而错误信息完全指不到真正的原因。
        """
        if path.startswith(CONTAINER_WORKSPACE):
            return path
        normalized = os.path.abspath(path)
        base = os.path.abspath(workspace)
        if normalized == base:
            return CONTAINER_WORKSPACE
        if normalized.startswith(base + os.sep):
            rel = normalized[len(base) + 1:].replace(os.sep, "/")
            return f"{CONTAINER_WORKSPACE}/{rel}"
        return CONTAINER_WORKSPACE

    # ------------------------------------------------------------------ kill
    def kill(self, runtime_id: str, *, reason: str = "") -> None:
        """杀掉容器内所有进程。**幂等** —— 容器已停/已删都不抛错。"""
        container = self._containers.get(runtime_id)
        if container is None:
            # 条目已经不在了，但容器可能还活着（比如 close 被并发调用），
            # 所以仍然盲目打一次 kill —— 反正失败也不抛错。
            self._run_docker(["docker", "kill", runtime_id])
            return

        if reason:
            container.kill_reason = reason
        container.dead = True
        if container.proc is not None:
            try:
                container.proc.kill()
            except OSError:
                pass
        code, _, err = self._run_docker(["docker", "kill", runtime_id])
        if code == 0 and reason:
            logger.info("docker sandbox killed: %s reason=%s", runtime_id[:12], reason)
        elif code != 0:
            logger.debug("docker kill %s 返回 %s: %s", runtime_id[:12], code, err.strip()[:200])

    # ----------------------------------------------------------------- close
    def close(self, runtime_id: str) -> None:
        """``docker rm -f`` 销毁容器并释放条目（§28 的 Destroy）。幂等。"""
        container = self._containers.pop(runtime_id, None)
        if container is not None:
            container.dead = True
        self._run_docker(["docker", "rm", "-f", runtime_id])

    # ----------------------------------------------------------------- 内部
    def _run_docker(self, args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
        """跑一条 docker CLI 命令，**不抛异常**。

        探测 / kill / rm 这些路径上，异常会打断清理流程；
        统一返回 ``(code, stdout, stderr)`` 让调用方按需处理。
        """
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except (OSError, subprocess.SubprocessError) as exc:
            return -1, "", str(exc)


class _Container:
    """一个沙箱容器的运行时状态。"""

    __slots__ = ("spec", "workspace", "proc", "dead", "kill_reason")

    def __init__(self, spec: SandboxSpec, workspace: str) -> None:
        self.spec = spec
        self.workspace = workspace
        # 当前正在跑的 docker exec 客户端；超时时要先把它摘掉再 kill 容器，
        # 否则 communicate() 会一直挂在已死的客户端上。
        self.proc: subprocess.Popen | None = None
        self.dead = False
        self.kill_reason = ""
