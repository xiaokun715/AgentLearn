"""ReportWorkspace —— 模拟 Agent 在临时目录里写报告文件产生的脏文件。

Agent 开始时在 ``workspace/<task_id>/`` 建一个临时工作区，逐步追加 ``report.md``；
正常完成时 :meth:`publish` 把成品复制到 ``results/`` 并删除临时工作区。
若中途被取消，supervisor 调 :meth:`cleanup` 删除残留的临时工作区 ——
对应第十章“删除临时文件”的兜底动作。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

DEMO_BASE = Path(tempfile.gettempdir()) / "graceful-agent-cancel-demo"


class ReportWorkspace:
    def __init__(self, task_id: str, base_dir: Path | None = None) -> None:
        base = Path(base_dir) if base_dir is not None else DEMO_BASE
        self.task_id = task_id
        self.work_dir: Path = base / "workspace" / task_id
        self.result_dir: Path = base / "results"
        self.report_path: Path = self.work_dir / "report.md"
        self._published: bool = False

    # ---- 生命周期 ------------------------------------------------------------
    def create(self) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text("", encoding="utf-8")
        return self.work_dir

    def append_line(self, line: str) -> None:
        with self.report_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def staged_files(self) -> list[str]:
        if not self.work_dir.exists():
            return []
        return [str(p) for p in self.work_dir.rglob("*") if p.is_file()]

    def publish(self) -> str:
        """成品定稿：复制到 results/ 并删除临时工作区。"""
        self.result_dir.mkdir(parents=True, exist_ok=True)
        final = self.result_dir / f"{self.task_id}.md"
        shutil.copyfile(self.report_path, final)
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self._published = True
        return str(final)

    @property
    def published(self) -> bool:
        return self._published

    def cleanup(self) -> dict | None:
        """取消兜底：删除残留临时工作区，返回被清理的文件；没有残留则返回 None。"""
        if not self.work_dir.exists():
            return None
        files = self.staged_files()
        shutil.rmtree(self.work_dir, ignore_errors=True)
        return {"removed_files": files, "work_dir": str(self.work_dir)}
