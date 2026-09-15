"""启动记忆系统观测 API 服务器。

运行：``python examples/live_server.py`` → http://localhost:8000
(等价于 ``uvicorn memory_engine.main:app --port 8000``)

打开 http://localhost:8000/docs 可直接在 Swagger 里点 endpoints 玩 8 原语与四层记忆。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import uvicorn

from memory_engine.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
