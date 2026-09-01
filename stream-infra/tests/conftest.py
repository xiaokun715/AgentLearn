"""pytest 公共 fixture。"""
from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from streaminfra.config import StreamConfig
from streaminfra.main import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def make_client(config: StreamConfig | None = None, **config_kwargs):
    """按自定义配置创建 (TestClient, app)。"""
    cfg = config or StreamConfig(**config_kwargs)
    app = create_app(config=cfg)
    return TestClient(app), app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server():
    """启动真实 uvicorn 服务器（用于验证真实的断线/背压行为）。

    ASGITransport 不会在客户端中途关闭连接时向应用发送 http.disconnect，
    因此断线/慢客户端这类场景必须用真实服务器来测。
    """
    running: list[tuple[threading.Thread, uvicorn.Server]] = []

    def _start(app, *, timeout: float = 10.0) -> str:
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.05)
        running.append((thread, server))
        return base

    yield _start

    for thread, server in running:
        server.should_exit = True
        thread.join(timeout=5)
