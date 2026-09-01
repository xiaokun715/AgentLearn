"""Filesystem Boundary（设计说明书 §15-16）。

原则：Default Deny —— Agent 只能操作 `/workspace`，且按 Policy 划分：
    /workspace/input   → 只读
    /workspace/output  → 可写
    /workspace/main.*  → 执行入口（由沙箱写入，Agent 看不到宿主机其余文件）

绝对不要 `-v /:/host`，否则容器 → 宿主文件系统，隔离直接失效。

宿主机侧每次执行都会创建一个独立临时目录，并把代码写进去；执行完 destroy 时整体删除
（§25 Ephemeral Runtime：1 execution = 1 ephemeral runtime）。
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CONTAINER_ROOT = "/workspace"

# 工具类型 → 工作区里代码文件名（Docker 容器内固定路径 /workspace/main.*）
CODE_NAMES: dict[str, str] = {
    "python": "main.py",
    "shell": "main.sh",
    "node": "main.js",
    "sql": "main.sql",
}


@dataclass(slots=True)
class Workspace:
    """一次执行对应的临时工作区（宿主机侧）。"""

    exec_id: str
    host_root: Path
    input_dir: Path
    output_dir: Path
    code_path: Path
    container_root: str = CONTAINER_ROOT

    @property
    def container_input(self) -> str:
        return f"{self.container_root}/input"

    @property
    def container_output(self) -> str:
        return f"{self.container_root}/output"

    @property
    def container_code(self) -> str:
        return f"{self.container_root}/{self.code_path.name}"

    @classmethod
    def create(cls, exec_id: str, tool_type: str, code: str) -> "Workspace":
        """在宿主机创建一个隔离工作区：input(只读) + output(可写) + 代码文件。"""
        code_name = CODE_NAMES.get(tool_type, "main.py")
        base = Path(tempfile.mkdtemp(prefix=f"sandbox_{exec_id[:8]}_"))
        input_dir = base / "input"
        output_dir = base / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        # Docker 里容器以非 root 的 sandbox 用户运行：输出目录必须 world-writable
        _chmod(output_dir, 0o777)
        _chmod(input_dir, 0o555)

        code_path = base / code_name
        code_path.write_text(code, encoding="utf-8")
        # 代码文件保持默认权限（POSIX 644，世界可读，容器内 sandbox 用户能读到）。
        # 注意：不要 chmod 成只读 —— Windows 上会把文件变成只读，导致 rmtree 删不掉。

        logger.debug("workspace created: %s (input=%s output=%s)",
                     base, input_dir, output_dir)
        return cls(
            exec_id=exec_id,
            host_root=base,
            input_dir=input_dir,
            output_dir=output_dir,
            code_path=code_path,
        )

    def cleanup(self) -> None:
        """无论成功 / 失败 / 超时 / kill，最后都必须调用（§26）。"""
        if not self.host_root.exists():
            return
        shutil.rmtree(self.host_root, ignore_errors=True)
        # Windows：rmtree 可能漏掉只读文件，先全部置可写再删（防御性回收）
        if self.host_root.exists():
            _make_writable(self.host_root)
            shutil.rmtree(self.host_root, ignore_errors=True)
        logger.debug("workspace cleaned: %s", self.host_root)


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # Windows 上 chmod 语义有限，忽略（Docker 里以 Linux 为准）
        pass


def _make_writable(root: Path) -> None:
    """递归把目录下所有文件/目录置为可写（Windows 清理前的防御措施）。"""
    for path in root.rglob("*"):
        try:
            os.chmod(path, 0o777)
        except OSError:
            pass
    try:
        os.chmod(root, 0o777)
    except OSError:
        pass
