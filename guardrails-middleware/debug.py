"""本地 debug 启动器（PyCharm 打断点友好）。

用法（在项目根目录执行）：
    python debug.py            # 默认关闭 reload，可直接打断点调试
    RELOAD=1 python debug.py   # 打开 uvicorn 热重载
    PORT=9000 python debug.py  # 换端口（默认 8000）

等价命令行：
    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import os
import sys

# 保证从任意目录执行都能 import 到本项目的 app 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn  # noqa: E402

APP_IMPORT = "app.main:app"
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
RELOAD = os.getenv("RELOAD", "0") == "1"
LOG_LEVEL = os.getenv("LOG_LEVEL", "info")


def main() -> None:
    uvicorn.run(
        APP_IMPORT,
        host=HOST,
        port=PORT,
        reload=RELOAD,
        log_level=LOG_LEVEL,
    )


if __name__ == "__main__":
    main()
