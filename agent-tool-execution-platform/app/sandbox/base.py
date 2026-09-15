"""Sandbox 抽象（说明书 §27 / §28）。

Sandbox 的职责只有一句：**给不可信的 Tool 代码划一个它跑不出去的圈。**
圈的内容就是 §27 那张表 —— CPU / Memory / Disk / Process / Network / Execution Time / Filesystem。

Demo 用 Docker Container（§28），生产换 Kubernetes Pod + Namespace + ResourceQuota +
NetworkPolicy + SecurityContext。所以这里抽成 :class:`Sandbox` 接口，
后端可插拔，上层（`app.sandbox.manager` / Tool）完全不感知区别。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..domain.policy import SandboxPolicy


@dataclass
class ExecResult:
    """一次沙箱内进程执行的结果。"""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    """被沙箱超时强杀 —— 对应 §29 的 ``Sandbox Timeout``。"""

    killed: bool = False
    """被显式 kill（取消 / Kill Switch）。"""

    resource_exhausted: bool = False
    """命中 CPU/Memory 限制（§27）—— 由 Recovery Policy 映射为 RESOURCE_EXHAUSTED。"""

    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.killed


@dataclass
class SandboxSpec:
    """创建一次沙箱运行时的入参。"""

    policy: SandboxPolicy
    workspace: str
    call_id: str = ""
    image: str = "python:3.10-slim"
    env: dict[str, str] = field(default_factory=dict)


class Sandbox(Protocol):
    """沙箱后端协议。

    刻意保持窄：只有 ``run`` / ``kill`` / ``close``。
    更复杂的编排（注入输入、收集输出、销毁）在 :mod:`app.sandbox.manager` 里做（§28）。
    """

    name: str

    def available(self) -> bool:
        """后端当前是否可用（Docker daemon 没起时返回 False，供 auto 退化判断）。"""
        ...

    def create(self, spec: SandboxSpec) -> str:
        """创建运行时，返回 ``runtime_id``。"""
        ...

    def run(
        self,
        runtime_id: str,
        argv: list[str],
        *,
        cwd: Optional[str] = None,
        stdin: str = "",
        timeout_seconds: Optional[float] = None,
    ) -> ExecResult:
        """在运行时里执行命令。"""
        ...

    def kill(self, runtime_id: str, *, reason: str = "") -> None:
        """终止运行时。**必须幂等** —— 重复 kill 不得抛错。"""
        ...

    def close(self, runtime_id: str) -> None:
        """销毁运行时，释放资源（§28 流程的最后一步 Destroy）。"""
        ...
