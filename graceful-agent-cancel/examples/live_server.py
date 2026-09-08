"""live demo —— 启动带红色 Stop 控制台的服务。

    python examples/live_server.py
    # 浏览器打开 http://localhost:8000/  就能玩那个 STOP 按钮控制台

等价于:  uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import uvicorn  # noqa: E402

from app.config import AgentConfig  # noqa: E402
from app.main import create_app  # noqa: E402

if __name__ == "__main__":
    cfg = AgentConfig()
    cfg.web_fetch_total = float(os.getenv("CANCEL_WEB_FETCH_TOTAL", "4.0"))
    app = create_app(config=cfg)
    print("\n  ▶ 打开 http://localhost:8000/  玩红色 Stop 按钮控制台")
    print("    curl -X POST http://localhost:8000/v1/tasks -H 'Content-Type: application/json' "
          "-d '{\"query\":\"查账单和天气\"}'\n")
    uvicorn.run(app, host="127.0.0.1", port=8000)
