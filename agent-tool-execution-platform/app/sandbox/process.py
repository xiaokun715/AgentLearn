"""Process Sandbox —— Docker 不可用时的**退化后端**（说明书 §27 / §28 / §29）。

它是 :class:`~app.sandbox.base.Sandbox` 协议的一个 stdlib 实现：
不依赖 Docker、不依赖 psutil，只用 ``subprocess`` + 平台能力。

隔离强度**弱于容器**，必须说清楚弱在哪：

    - 文件系统：没有 mount namespace，Tool 能读写宿主机上该用户可及的任何路径。
      本后端只能靠「工作目录 + 环境变量最小化」降低误伤概率，挡不住恶意代码。
    - 网络：没有 network namespace，断网只能靠不注入代理变量，**挡不住真的联网**。
    - 内核：共享内核，提权漏洞直接影响宿主机。

它能提供的三样是真实的、可验证的：

    - **超时隔离**（§29）：到期 kill 整个进程组，不留孤儿进程继续产生副作用。
    - **资源隔离**（§27）：POSIX 上用 ``setrlimit`` 施加 CPU / 内存 / 磁盘 / 进程数上限。
    - **环境隔离**：不继承宿主 ``os.environ``，密钥不会顺着环境变量漏进 Tool。

因此它适合 Demo / 本地开发 / CI；生产请用 Docker（:mod:`app.sandbox.docker`）
或更强的 gVisor / Firecracker / K8s Pod（§28）。
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import sys
import threading

from ..domain.errors import SandboxError
from ..infra.clock import Clock, SystemClock
from .base import ExecResult, SandboxSpec

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
IS_POSIX = os.name == "posix"

# 超时强杀时约定的退出码（POSIX 上进程被 SIGKILL 也是 -9，保持一致便于上层判断）
TIMEOUT_EXIT_CODE = -9

# ---------------------------------------------------------------------------
# 环境变量白名单
#
# 默认**空环境**：绝不 ``dict(os.environ)``。宿主机上常常挂着
# ``OPENAI_API_KEY`` / ``AWS_SECRET_ACCESS_KEY`` / ``GITHUB_TOKEN``，
# 一旦整份继承，Tool 只要 ``print(os.environ)`` 就能把它们读出来 ——
# 而 Tool 的代码是 LLM 生成的，它不该有这个机会。
#
# 下面这几个是**平台必需、且不含凭据**的例外，缺了它们子进程在某些系统上
# 根本起不来（Windows 上 ``SystemRoot`` 是加载 CRT / Winsock 的前置），
# 所以是按名字白名单进来，而不是整份放开。
# ---------------------------------------------------------------------------
_ESSENTIAL_ENV_POSIX = ("PATH", "LANG", "LC_ALL", "TZ")
_ESSENTIAL_ENV_WIN32 = (
    "PATH",
    "SystemRoot",
    "windir",
    "ComSpec",
    "PATHEXT",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "OS",
)

# 强制注入的解释器行为变量：
#   PYTHONIOENCODING=utf-8 —— 管道默认编码在 Windows 上是 GBK，中文输出会乱码/报错
#   PYTHONDONTWRITEBYTECODE —— 只读挂载下写 __pycache__ 会直接 PermissionError
#   PYTHONUNBUFFERED —— 否则超时被杀时缓冲区里的输出全部丢失，只剩空 stdout
#   PYTHONNOUSERSITE —— 挡住宿主用户目录里 pip install --user 的包（隔离，也防版本污染）
_PYTHON_ENV = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONNOUSERSITE": "1",
}


class _Runtime:
    """一个沙箱运行时的状态。

    ``lock`` 是给 ``kill`` 用的：Kill Switch 和超时强杀可能从两个线程同时到达
    同一个 runtime，没有它的话「先判断再杀」之间会有窗口，第二次 kill 就可能
    打在一个已经被回收的 pid 上（pid 复用 → 杀错进程）。
    """

    __slots__ = ("spec", "policy", "workspace", "env", "proc", "killed", "kill_reason", "lock")

    def __init__(self, spec: SandboxSpec, env: dict[str, str]) -> None:
        self.spec = spec
        self.policy = spec.policy
        self.workspace = spec.workspace
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.killed = False
        self.kill_reason = ""
        self.lock = threading.Lock()


class ProcessSandbox:
    """子进程后端。:attr:`name` 固定为 ``"process"``，供 Manager 的 auto 退化识别。"""

    name = "process"

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or SystemClock()
        self._runtimes: dict[str, _Runtime] = {}
        # create / close / run 可能被不同线程调用（Worker 各自 open 自己的沙箱），
        # 所以对 dict 的增删要加锁 —— CPython 的 dict 单次操作是原子的，
        # 但「检查再插入」不是。
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 能力
    def available(self) -> bool:
        """永远可用 —— 本后端的存在意义就是「Docker 不在时的兜底」。"""
        return True

    # ---------------------------------------------------------------- create
    def create(self, spec: SandboxSpec) -> str:
        """登记一个运行时，返回 ``sbx_<hex8>``。

        刻意**不在这里起进程**：协议是 ``create -> run -> kill -> close``，
        一个 runtime 可能被 ``run`` 多次（例如先跑测试再跑覆盖率），
        进程应该在 ``run`` 时按需起、跑完即收，而不是先挂一个空闲进程占着资源。
        """
        runtime_id = f"sbx_{secrets.token_hex(4)}"
        runtime = _Runtime(spec, env=build_minimal_env(spec.env))
        with self._lock:
            self._runtimes[runtime_id] = runtime
        logger.debug(
            "process sandbox created: %s call_id=%s workspace=%s",
            runtime_id, spec.call_id, spec.workspace,
        )
        return runtime_id

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
        """在工作区里跑一条命令，同步等待到结束 / 超时。"""
        runtime = self._get(runtime_id)
        if runtime.killed:
            # 已经被 kill 的 runtime 不再执行：取消之后进来的 run 必须失败，
            # 否则「取消」就只是杀掉了上一个进程，下一个照跑不误。
            return ExecResult(
                exit_code=-1,
                stderr=f"runtime {runtime_id} 已被 kill，拒绝再执行",
                killed=True,
                meta={"backend": self.name, "reason": runtime.kill_reason},
            )

        timeout = self._effective_timeout(runtime, timeout_seconds)
        workdir = cwd or runtime.workspace
        started = self._clock.time()

        popen_kwargs = self._popen_kwargs(runtime, workdir, timeout)
        try:
            proc = subprocess.Popen(argv, **popen_kwargs)  # type: ignore[arg-type]
        except (OSError, ValueError) as exc:
            # 命令不存在 / 不可执行 —— 这是 Tool 参数问题，不是平台崩溃，
            # 包成 SandboxError 让 Recovery Policy 能按 SANDBOX_ERROR 处置。
            raise SandboxError(
                f"沙箱内启动进程失败: {argv!r} ({exc})",
                detail={"argv": argv, "workspace": workdir},
            ) from exc

        with runtime.lock:
            runtime.proc = proc

        return self._wait(proc, runtime, timeout, started, stdin)

    def _effective_timeout(self, runtime: _Runtime, requested: float | None) -> float:
        """``min(调用方给的, policy 上限)``。

        取 min 而不是「调用方说了算」：调用方（Tool 代码 / LLM 填的参数）是
        **不可信输入**，它把 timeout 填成 86400 也不能突破 §27 里声明的沙箱上限。
        """
        limit = float(runtime.policy.timeout_seconds)
        if requested is None:
            return limit
        return max(0.1, min(float(requested), limit))

    def _wait(
        self,
        proc: subprocess.Popen,
        runtime: _Runtime,
        timeout: float,
        started: float,
        stdin: str,
    ) -> ExecResult:
        timed_out = False
        try:
            stdout, stderr = proc.communicate(stdin or None, timeout=timeout)
        except subprocess.TimeoutExpired:
            # §29：超时必须**真正杀掉**，不能只是停止等待。
            # 只 stop waiting 的话进程还在跑，它可能已经/正在产生副作用，
            # 而平台已经返回 TIMEOUT 并可能重试 —— 副作用叠两层。
            timed_out = True
            self._kill_tree(runtime, proc, reason=f"timeout>{timeout}s")
            stdout, stderr = self._drain(proc)

        duration_ms = int((self._clock.time() - started) * 1000)
        returncode = proc.returncode

        if timed_out:
            exit_code = TIMEOUT_EXIT_CODE
        elif returncode is None:
            exit_code = -1
        else:
            exit_code = returncode

        meta: dict = {
            "backend": self.name,
            "effective_timeout_s": timeout,
            "duration_ms": duration_ms,
            "pid": proc.pid,
            "returncode": returncode,
        }
        meta.update(self._resource_meta(runtime, timeout))

        with runtime.lock:
            runtime.proc = None
            # 与 kill() 在同一把锁下读：Kill Switch（§41 取消）可能就在
            # communicate() 返回的这一刻打进来。
            explicitly_killed = runtime.killed

        exhausted = False
        if not timed_out and not explicitly_killed and returncode is not None and returncode < 0:
            # POSIX 下 returncode 为负 = 被信号杀死。SIGXCPU / SIGXFSZ 是我们
            # 用 setrlimit 设的软限制打出来的；SIGKILL 则可能是 OOM killer 或
            # 内存硬限制。这三种都归到 §27 的 RESOURCE_EXHAUSTED，好让 Recovery
            # Policy 走「降配重试」而不是「当作业务失败」。
            #
            # 但「被平台自己 kill」必须排除在外：取消一次正在跑的执行同样会让
            # returncode = -SIGKILL，若误判成资源耗尽，Recovery Policy 会按
            # RESOURCE_EXHAUSTED 去**重试一次已经被取消的执行** —— 取消反而
            # 变成了「再跑一遍」。
            sig = -returncode
            exhausted = sig in _RESOURCE_SIGNALS
            if exhausted:
                meta["signal"] = sig
                meta["signal_name"] = _signal_name(sig)

        if explicitly_killed:
            meta["kill_reason"] = runtime.kill_reason

        return ExecResult(
            exit_code=exit_code,
            stdout=_decode(stdout),
            stderr=_decode(stderr),
            duration_ms=duration_ms,
            timed_out=timed_out,
            # killed 专指「被平台显式 kill（取消 / Kill Switch）」。
            # 超时不算取消 —— 这两者在 §35 的 Recovery 表里处置不同。
            # 注意这里包含「命令已正常结束、但取消请求刚好同时到达」的窄竞态：
            # 此时报 killed=True（ok=False）是刻意保守的选择 ——
            # 取消赢了竞态，上层就该按「结果不可信」处理，而不是当成功落库。
            killed=explicitly_killed,
            resource_exhausted=exhausted,
            meta=meta,
        )

    def _drain(self, proc: subprocess.Popen) -> tuple[bytes | str, bytes | str]:
        """杀掉进程后把管道里剩下的输出收干净。

        进程组被 SIGKILL 后管道必然关闭，正常情况瞬间返回；但若某个后代进程
        逃出了进程组还攥着管道写端，``communicate`` 会一直等 —— 超时分支里的
        二次等待必须有上限，否则「超时处理」自己变成新的挂死点。
        """
        try:
            return proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            logger.warning("沙箱进程 %s 的输出管道未能收干，放弃采集", proc.pid)
            return b"", b""

    # -------------------------------------------------------------- 进程启动
    def _popen_kwargs(self, runtime: _Runtime, workdir: str, timeout: float) -> dict:
        """组装 ``Popen`` 参数 —— 平台差异都在这里收口。"""
        kwargs: dict = {
            "cwd": workdir,
            "env": runtime.env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            # text + utf-8：沙箱内的输出可能是任意编码，errors="replace" 保证
            # 一份乱码的 stdout 不会把整个执行变成 UnicodeDecodeError。
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if IS_POSIX:
            # start_new_session=True 让子进程成为新会话/进程组组长，
            # 于是超时可以用 os.killpg 一次干掉整棵树（含孙进程）。
            # 否则杀掉 python 父进程后，它 fork 出来的子进程会变成孤儿继续跑。
            kwargs["start_new_session"] = True
            kwargs["preexec_fn"] = _make_rlimit_preexec(runtime.policy, timeout)
        elif IS_WINDOWS:
            flags = 0
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 不弹控制台窗口
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)  # 便于整组终止
            kwargs["creationflags"] = flags
        return kwargs

    def _resource_meta(self, runtime: _Runtime, timeout: float) -> dict:
        """把「到底有没有施加资源限制」如实记进 meta。

        Windows 上没有 ``resource`` 模块，本后端**优雅降级**而不是崩掉：
        §27 的限制在这里变成「未施加」，上层看到这个标记就知道这次执行的
        资源隔离没生效（审计需要这个信息，不能假装限制了）。
        """
        if not IS_POSIX:
            return {"resource_limits": "unsupported-on-win32", "platform": sys.platform}
        return {
            "resource_limits": "applied",
            "platform": sys.platform,
            "rlimits": _describe_rlimits(runtime.policy, timeout),
        }

    # ------------------------------------------------------------------ kill
    def kill(self, runtime_id: str, *, reason: str = "") -> None:
        """终止运行时。**幂等** —— 重复 kill、kill 已退出/不存在的 runtime 都不抛错。

        幂等不是「顺手加的」：Kill Switch（§41 取消）、超时强杀、Worker 崩溃回收
        三条路径都会打到这里，其中任意两条可能同时发生，还可能是对同一个
        已经被销毁的 runtime 重放（消息重投）。任何一次抛错都会让调用方卡在
        「不知道杀没杀掉」的状态上。
        """
        runtime = self._runtimes.get(runtime_id)
        if runtime is None:
            return  # 已 close / 从未存在 → 幂等返回
        with runtime.lock:
            if reason:
                runtime.kill_reason = reason
            runtime.killed = True
            proc = runtime.proc
            if proc is None or proc.poll() is not None:
                return  # 进程已退出 → 幂等返回
            self._kill_tree(runtime, proc, reason=reason)

    def _kill_tree(self, runtime: _Runtime, proc: subprocess.Popen, *, reason: str = "") -> None:
        """杀掉整个进程组 / 进程树，并回收僵尸进程。"""
        pid = proc.pid
        try:
            if IS_POSIX:
                # killpg 而不是 proc.kill()：只杀父进程会留下孙进程，
                # 而孙进程往往才是真正在干活的（Tool 常 fork 出子命令）。
                os.killpg(os.getpgid(pid), 9)
            elif IS_WINDOWS:
                # Windows 没有进程组信号，用 taskkill /T 递归整棵树。
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
        except (ProcessLookupError, PermissionError, OSError) as exc:
            # 进程已经自己消失（最常见的正常路径）→ 不是错误。
            logger.debug("kill 进程树 %s 时进程已不存在: %s", pid, exc)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况
            logger.warning("taskkill 超时: pid=%s", pid)

        # 兜底直接打父进程：killpg 失败（如进程组已被回收但进程仍在）时补刀。
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass

        try:
            # 必须 wait：否则子进程变僵尸，长期运行会把 pid 表耗尽。
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("沙箱进程 %s 被杀后仍未回收", pid)

        if reason:
            logger.info("process sandbox killed: pid=%s reason=%s", pid, reason)

    # ----------------------------------------------------------------- close
    def close(self, runtime_id: str) -> None:
        """销毁运行时并释放条目（§28 流程的最后一步 Destroy）。幂等。"""
        runtime = self._runtimes.get(runtime_id)
        if runtime is None:
            return
        self.kill(runtime_id, reason="close")
        with self._lock:
            self._runtimes.pop(runtime_id, None)

    # ----------------------------------------------------------------- 内部
    def _get(self, runtime_id: str) -> _Runtime:
        runtime = self._runtimes.get(runtime_id)
        if runtime is None:
            raise SandboxError(
                f"沙箱运行时不存在或已销毁: {runtime_id}",
                detail={"runtime_id": runtime_id},
            )
        return runtime


# --------------------------------------------------------------------------- #
# 模块级工具
# --------------------------------------------------------------------------- #
def build_minimal_env(declared: dict[str, str] | None = None) -> dict[str, str]:
    """组装子进程环境：**空环境** + 平台必需项 + 显式声明 + Python 行为变量。

    ``declared`` 放最后，让显式声明能覆盖掉我们的默认值（例如 Tool 想关掉
    ``PYTHONUNBUFFERED``）。但 ``PATH`` 这类平台必需项若被显式声明覆盖，
    也以声明为准 —— 调用方明确要什么就给什么。
    """
    names = _ESSENTIAL_ENV_WIN32 if IS_WINDOWS else _ESSENTIAL_ENV_POSIX
    env: dict[str, str] = {}
    for name in names:
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update(_PYTHON_ENV)
    if declared:
        env.update({str(k): str(v) for k, v in declared.items()})
    return env


def _decode(raw: bytes | str | None) -> str:
    """统一输出解码；Windows 管道会把 ``\\n`` 写成 ``\\r\\n``，归一化掉。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = raw
    return text.replace("\r\n", "\n")


_RESOURCE_SIGNALS: tuple[int, ...] = ()


def _init_resource_signals() -> tuple[int, ...]:
    """被 setrlimit 打出来的信号集合（仅 POSIX 有）。"""
    if not IS_POSIX:
        return ()
    import signal

    # SIGXCPU = RLIMIT_CPU 软限到点；SIGXFSZ = RLIMIT_FSIZE 超限；
    # SIGKILL = OOM killer / 内存硬限（SIGKILL 也可能是外部 kill，
    # 所以只在「没超时且没被显式 kill」时才当资源耗尽处理，见 _wait）。
    return (signal.SIGXCPU, signal.SIGXFSZ, signal.SIGKILL)


_RESOURCE_SIGNALS = _init_resource_signals()


def _signal_name(sig: int) -> str:
    if not IS_POSIX:
        return str(sig)
    import signal

    try:
        return signal.Signals(sig).name
    except ValueError:  # pragma: no cover
        return str(sig)


def _make_rlimit_preexec(policy, timeout: float):
    """生成 ``preexec_fn``：在 ``exec`` 之前给子进程施加 §27 的资源上限。

    为什么用 ``preexec_fn``：stdlib 里没有别的办法在 fork 与 exec 之间设置
    rlimit（``resource.setrlimit`` 只能作用于当前进程或其子进程）。
    已知代价：``preexec_fn`` 在多线程父进程里理论上有死锁风险；
    本平台是「每 Worker 一线程 + 每沙箱一子进程」的同步模型，
    实际路径上 fork 期间其他线程极少持有 malloc 锁，可以接受。
    生产要彻底规避这个风险，就该换容器后端（rlimit 由 runc 在容器 init 里设）。

    ``RLIMIT_CPU`` 的语义要特别注意：它是**累计 CPU 秒**，不是核数上限。
    容器里的 ``--cpus`` 是速率限制，二者不是一回事。这里按
    「核数 × 墙钟上限」折算成累计 CPU 秒 —— 2 核跑满 310s ⇒ 620 CPU 秒，
    给工具留出用满多核的余地，同时保证单核死循环一定在墙钟超时附近被 SIGXCPU 打断。

    另外两处 rlimit 的坑，用之前要知道：

    - ``RLIMIT_NPROC`` 是**按 UID 计数**的，不是按进程组。在共享账号上
      （CI runner、同机多 Worker 同用户）已存在的进程会一起算进上限，
      配额给小了会让良性的 ``fork`` 直接 EAGAIN。所以这里只把它当
      「拦住指数级 fork bomb」的粗栏杆，不指望它做精确配额。
    - ``RLIMIT_AS`` 限制的是**虚拟地址空间**。像 JVM / Go runtime 这类会
      预留大块虚拟内存的运行时，即使实际 RSS 远低于上限也会申请失败 ——
      撞上这种情况该调大 ``memory_mb``，而不是以为内存限制没生效。
    """
    import resource as _resource  # 只可能在 POSIX 路径上被调用

    cpu_seconds = max(1, int(policy.cpu * timeout) + 1)
    memory_bytes = policy.memory_mb * 1024 * 1024
    disk_bytes = policy.disk_mb * 1024 * 1024
    max_procs = policy.max_processes

    def _apply() -> None:
        limits = [
            (_resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 5),
            (_resource.RLIMIT_AS, memory_bytes, memory_bytes),
            (_resource.RLIMIT_FSIZE, disk_bytes, disk_bytes),
            (_resource.RLIMIT_NPROC, max_procs, max_procs),
            # core dump 关掉：Tool 崩掉时写下的 core 文件可能有几百 MB，
            # 既撑爆磁盘配额，也可能把内存里的敏感数据落到工作区里。
            (_resource.RLIMIT_CORE, 0, 0),
        ]
        for what, soft, hard in limits:
            try:
                _resource.setrlimit(what, (soft, hard))
            except (ValueError, OSError):
                # 单条设置失败（如容器里 RLIMIT_NPROC 已锁死）不该让整次执行失败，
                # 剩下的限制照常生效。
                pass

    return _apply


def _describe_rlimits(policy, timeout: float) -> dict[str, int]:
    """把折算结果记进 meta —— 排查「到底限制到了多少」时不用去读代码。"""
    return {
        "RLIMIT_CPU": max(1, int(policy.cpu * timeout) + 1),
        "RLIMIT_AS": policy.memory_mb * 1024 * 1024,
        "RLIMIT_FSIZE": policy.disk_mb * 1024 * 1024,
        "RLIMIT_NPROC": policy.max_processes,
    }
