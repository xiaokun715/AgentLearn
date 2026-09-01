"""示例：向沙箱提交一段 Shell 命令（设计说明书 §14 shell 工具）。

先启动服务：
    uvicorn app.main:app --port 8000

再运行：
    python examples/shell.py
"""
from __future__ import annotations

import sys
import time

import httpx

from python import submit  # 复用上面的轮询 helper

if __name__ == "__main__":
    # 用相对路径 output/ —— Docker 下 cwd=/workspace，ProcessSandbox 下 cwd=工作区根，
    # 两者都指向同一个 output 目录（§15 只允许操作工作区）
    view = submit(
        "shell",
        "echo hello from sandbox && pwd && ls -la output",
        policy={"name": "shell_basic"},
    )
    print(f"status    : {view['status']}")
    print(f"stdout    :\n{view['stdout']}")
    print(f"stderr    :\n{view['stderr']}")
    print(f"exit_code : {view['exit_code']}")
    sys.exit(0 if view["status"] == "succeeded" else 1)
