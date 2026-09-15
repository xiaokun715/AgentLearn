"""Sandbox Manager —— 沙箱生命周期的编排者（说明书 §28）。

§28 给出的执行流程，本模块就是它的实现：

::

    Tool
     ↓
    Sandbox Manager          ← open()：解析策略 + 准备 workspace + 选后端
     ↓
    Create Runtime           ← backend.create(spec) -> runtime_id
     ↓
    Inject Input             ← handle.run(argv, stdin=...) 把输入交给沙箱内进程
     ↓
    Execute                  ← 后端内部：超时兜底 + 资源限制 + 环境最小化
     ↓
    Collect Output           ← ExecResult（stdout/stderr/exit_code/duration_ms/meta）
     ↓
    Destroy                  ← close() / cancel()：kill + 释放槽位

三条设计原则：

1. **上层只看见 Handle，看不见后端。** Tool 拿到的
   :class:`SandboxHandle` 只知道「往哪儿跑、按什么策略跑」，
   换 Docker / process 后端对它完全透明（:class:`~app.sandbox.base.Sandbox` 协议）。
2. **句柄即授权。** 策略在 ``open()`` 时定死并绑进 handle，
   Tool 中途拿不到「把 timeout 调大」「把 network 打开」的入口。
3. **有开必有关。** 并发槽位在 ``open()`` 记账、在 ``close()/cancel()`` 释放，
   两条路径互斥（谁先移除 ``_active`` 里的条目谁负责释放），
   所以重复 close / cancel 不会把信号量额度放多。
"""
from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass, field
from typing import Optional

from ..config import AppConfig
from ..domain.errors import SandboxError
from ..domain.policy import SandboxPolicy
from ..infra.clock import Clock, SystemClock
from .base import ExecResult, Sandbox, SandboxSpec
from .docker import DockerSandbox
from .policy import ensure_workspace, resolve_policy
from .process import ProcessSandbox

logger = logging.getLogger(__name__)


@dataclass
class SandboxHandle:
    """绑定好 policy 的沙箱执行句柄，交给 :class:`~app.tools.base.ToolContext` 使用。

    Tool 侧只需 ``ctx.run_in_sandbox = handle.run`` —— 签名与
    :meth:`ToolContext.sandbox_run` 完全一致。
    """

    sandbox_id: str
    """平台侧句柄 ID，用于日志 / 事件 / 审计串联。"""

    runtime_id: str
    """后端侧运行时 ID（容器 ID / ``sbx_xxxx``），只有 Manager 用得上。"""

    backend: str
    workspace: str
    policy: SandboxPolicy

    _sandbox: Optional[Sandbox] = field(default=None, repr=False)
    """后端引用。私有字段：Tool 不该绕过 handle 直接操作后端。"""

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        stdin: str = "",
        timeout_seconds: float | None = None,
    ) -> ExecResult:
        """在沙箱里执行 ``argv``。

        ``cwd`` 缺省落在工作区：Tool 里写的相对路径因此不会踩到后台进程的
        启动目录（那是宿主机的任意路径）。
        """
        if self._sandbox is None:  # pragma: no cover - 只可能是手工构造的 handle
            raise SandboxError("SandboxHandle 未绑定后端，无法执行")
        return self._sandbox.run(
            self.runtime_id,
            argv,
            cwd=cwd or self.workspace,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
        )


class SandboxManager:
    """按 §28 的流程编排沙箱的创建、执行、销毁与取消。

    :param config: 平台配置（``sandbox_backend`` / ``default_sandbox`` /
        ``workspace_root`` / ``max_concurrency``）
    :param clock: 可注入时钟，让超时相关的行为可被推演
    :param backend: 显式指定后端名，覆盖 ``config.sandbox_backend``（测试用）
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        clock: Clock | None = None,
        backend: str | None = None,
    ) -> None:
        self.config = config
        self._clock: Clock = clock or SystemClock()
        self._requested_backend = backend or config.sandbox_backend

        # 后端**惰性**创建：DockerSandbox 的构造不碰 daemon，但 available()
        # 要开子进程；manager 构造阶段不该有这种副作用（可能只是被 import 一下）。
        self._backend: Sandbox | None = None
        self._active: dict[str, SandboxHandle] = {}

        # 并发度控制（§48 Worker 并发）：用信号量而不是计数器，
        # 超额的 open() 会**在这里阻塞**等待槽位，而不是让宿主机被
        # N 个沙箱同时压垮。这是背压真正生效的那一点。
        self._sem = threading.Semaphore(max(1, config.max_concurrency))

    # ------------------------------------------------------------------ 后端
    def get_backend(self) -> Sandbox:
        """按 §28 + ``configs/sandbox.yaml`` 的 backend 语义选择后端（结果缓存）。

        - ``process`` —— 直接用子进程后端
        - ``docker``  —— 强制容器，不可用就抛错（不静默降级）
        - ``auto``    —— 先试 Docker，不可用则**退化到 process 并记 warning**

        ``docker`` 与 ``auto`` 的区别就是「能不能接受隔离强度变弱」：
        auto 允许，但必须吼一声，让运维在日志里看得见。
        """
        if self._backend is not None:
            return self._backend

        requested = (self._requested_backend or "auto").strip().lower()
        if requested == "process":
            self._backend = ProcessSandbox(clock=self._clock)
        elif requested == "docker":
            docker = DockerSandbox(clock=self._clock)
            if not docker.available():
                raise SandboxError(
                    "sandbox_backend=docker 但 Docker 不可用；"
                    "请启动 daemon，或改用 process / auto",
                    detail={"backend": "docker"},
                )
            self._backend = docker
        elif requested == "auto":
            docker = DockerSandbox(clock=self._clock)
            if docker.available():
                self._backend = docker
            else:
                # 隔离强度真的变弱了，必须让人看见 —— 这条 warning 是
                # 「生产环境误跑在 process 后端上」的唯一告警机会。
                logger.warning(
                    "Docker 不可用，沙箱后端自动退化到 process —— "
                    "隔离强度弱于容器（无文件系统/网络 namespace），"
                    "仅适用于开发与 CI，生产请修复 Docker"
                )
                self._backend = ProcessSandbox(clock=self._clock)
        else:
            raise SandboxError(
                f"未知的 sandbox_backend: {requested!r}（可选 auto / docker / process）",
                detail={"backend": requested},
            )

        logger.info("沙箱后端就绪: %s", self._backend.name)
        return self._backend

    # ------------------------------------------------------------------ 打开
    def open(
        self,
        *,
        call_id: str,
        policy: SandboxPolicy | None = None,
        tool_name: str = "",
        timeout_ms: int | None = None,
    ) -> SandboxHandle:
        """创建一次沙箱执行，返回绑定好策略的句柄。

        :param call_id: 本次调用 ID —— 同时决定工作区目录名（会被清洗）
        :param policy: 显式策略；缺省用 ``config.effective_sandbox(tool_name)``
        :param tool_name: 用于从 ``configs/tools.yaml`` 取该 Tool 的策略覆盖（§59）
        :param timeout_ms: 该 Tool 声明的超时（§21），沙箱超时会按 §29 抬到它之上
        """
        base = policy or self.config.effective_sandbox(tool_name) or self.config.default_sandbox
        effective = resolve_policy(base, timeout_ms=timeout_ms)

        # 工作区先于槽位准备：它是纯本地操作，不占并发额度，
        # 放在 acquire 之后会让「等槽位」的时间白占一个已建目录的语义。
        workspace = ensure_workspace(self.config.workspace_root, call_id)

        self._acquire_slot(call_id)
        try:
            backend = self.get_backend()
            # image 不在这里指定：它属于「后端怎么实现」而不是「策略允许多少」，
            # process 后端根本用不到它。要换镜像就换 DockerSandbox(image=...)。
            spec = SandboxSpec(
                policy=effective,
                workspace=workspace,
                call_id=call_id,
            )
            runtime_id = backend.create(spec)
        except BaseException:
            # create 失败 / 后端选择失败 → 必须把刚记的额度还回去，
            # 否则每失败一次，平台就永久少一个并发槽（最终彻底卡死）。
            self._sem.release()
            raise

        handle = SandboxHandle(
            sandbox_id=f"sbx_{secrets.token_hex(4)}",
            runtime_id=runtime_id,
            backend=backend.name,
            workspace=workspace,
            policy=effective,
            _sandbox=backend,
        )
        self.track(call_id, handle)
        if self.config.metrics_enabled:
            logger.debug(
                "沙箱已打开 sandbox_id=%s backend=%s call_id=%s active=%d",
                handle.sandbox_id, handle.backend, call_id, self.active_count(),
            )
        return handle

    def _acquire_slot(self, call_id: str) -> None:
        """占用一个并发槽位；满了就阻塞等待，并把等待这件事记进日志。

        先非阻塞试一次只是为了**可观测**：没有这条日志，
        「平台变慢」和「平台在排队」在外部看起来一模一样。
        """
        if self._sem.acquire(blocking=False):
            return
        logger.info(
            "沙箱并发已达上限 %d，call_id=%s 排队等待槽位",
            self.config.max_concurrency, call_id,
        )
        self._sem.acquire()

    # ------------------------------------------------------------------ 关闭
    def close(self, handle: SandboxHandle) -> None:
        """销毁句柄对应的运行时并释放槽位（§28 的 Destroy）。幂等。

        「谁摘掉 ``_active`` 里的条目，谁负责释放槽位」——
        这条约定让 close / cancel 无论以什么顺序、重复多少次调用，
        信号量额度都只会被释放一次。
        """
        key = self._drop(handle)
        try:
            self.get_backend().close(handle.runtime_id)
        except SandboxError as exc:
            # 运行时可能已经不在了（被超时强杀后自动销毁）→ 不影响槽位释放。
            logger.debug("关闭沙箱 %s 时后端报错: %s", handle.runtime_id, exc)
        finally:
            if key is not None:
                self._sem.release()

    def kill(self, handle: SandboxHandle, *, reason: str = "") -> None:
        """终止运行时但**不释放句柄**（取消 ≠ 销毁）。

        和 close 分开是因为调用方常常需要「先杀、再读一次已经产生的输出、
        最后才 close」—— 例如超时后想拿到 partial stdout 落进 artifact。
        幂等由后端协议保证。
        """
        try:
            self.get_backend().kill(handle.runtime_id, reason=reason)
        except SandboxError as exc:
            logger.debug("kill 沙箱 %s 时后端报错: %s", handle.runtime_id, exc)

    # ------------------------------------------------------------------ 登记
    def track(self, call_id: str, handle: SandboxHandle) -> None:
        """把句柄登记到 ``call_id`` 上，供取消 / 查询使用。"""
        self._active[call_id] = handle

    def handle_for(self, call_id: str) -> SandboxHandle | None:
        """按 ``call_id`` 查正在运行的沙箱 —— 取消链路（§41）需要它。"""
        return self._active.get(call_id)

    def cancel(self, call_id: str, *, reason: str = "cancelled") -> bool:
        """取消一次执行：kill 正在跑的沙箱并销毁它。

        返回**是否找到了**这个 call_id。找不到返回 ``False`` 而不是抛错：
        取消是并发下最容易被重复投递的操作（Kill Switch + 用户点两次 +
        Reaper 清理），「已经没在跑了」对调用方来说就是成功。
        """
        handle = self._active.get(call_id)
        if handle is None:
            return False
        try:
            self.kill(handle, reason=reason)
        finally:
            self.close(handle)
        logger.info("已取消沙箱执行 call_id=%s sandbox_id=%s reason=%s",
                    call_id, handle.sandbox_id, reason)
        return True

    def active_count(self) -> int:
        """当前存活（已 open 未 close）的沙箱数。"""
        return len(self._active)

    # ------------------------------------------------------------------ 内部
    def _drop(self, handle: SandboxHandle) -> str | None:
        """按**对象身份**摘掉登记项，返回被摘掉的 call_id；不在登记表里返回 None。

        用身份而不是 ``runtime_id`` 比对：句柄可能在 cancel 之后又被
        close 一次，此时它已不在表里，必须返回 None 才能保证槽位不被二次释放。
        """
        for call_id, tracked in list(self._active.items()):
            if tracked is handle:
                self._active.pop(call_id, None)
                return call_id
        return None
