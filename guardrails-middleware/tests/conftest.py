"""共享 fixtures：从 configs/ 构建一份全新 Guardrails（隔离 metrics/audit）。"""
from __future__ import annotations

import httpx
import pytest

from app.factory import build_guardrails


@pytest.fixture
def g():
    """干净的 Guardrails 实例（每个测试独立，避免 metrics/audit 互相污染）。"""
    return build_guardrails()


@pytest.fixture
async def client(g):
    from app.main import create_app

    app = create_app(guardrails=g)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
