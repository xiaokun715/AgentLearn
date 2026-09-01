"""HTTP API 测试（设计说明书 §30）：用 TestClient 走完整流程。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import RegistryConfig
from app.factory import build_runtime
from app.main import create_app

AGENT = "test_case_agent"


@pytest.fixture
def client():
    import asyncio

    async def _build():
        rt = await build_runtime(
            RegistryConfig(storage_backend="memory", cache_backend="memory")
        )
        app = create_app(runtime=rt)
        return rt, app

    rt, app = asyncio.run(_build())
    with TestClient(app) as c:
        c.rt = rt
        yield c


def _prompt_version_body(template, **kw):
    body = {"template": template, "created_by": "alice"}
    body.update(kw)
    return body


def _config_body(prompt_version, **kw):
    body = {
        "prompt": {"name": AGENT, "version": prompt_version},
        "model": {"provider": "qwen", "name": "qwen3.5-27b"},
        "parameters": {"temperature": 0.2, "max_tokens": 4096},
        "tools": {"version": 5},
        "created_by": "alice",
    }
    body.update(kw)
    return body


def test_prompt_crud_over_http(client):
    r = client.post("/v1/prompts", json={"name": AGENT, "created_by": "alice"})
    assert r.status_code == 201

    r = client.post(f"/v1/prompts/{AGENT}/versions", json=_prompt_version_body("v1 内容"))
    assert r.status_code == 201
    assert r.json()["version"] == 1

    r = client.post(f"/v1/prompts/{AGENT}/versions", json=_prompt_version_body("v2 内容"))
    assert r.json()["version"] == 2

    r = client.get(f"/v1/prompts/{AGENT}/versions")
    versions = r.json()
    assert [v["version"] for v in versions] == [1, 2]


def test_config_crud_over_http(client):
    client.post("/v1/prompts", json={"name": AGENT})
    client.post(f"/v1/prompts/{AGENT}/versions", json=_prompt_version_body("v1 内容"))

    r = client.post(f"/v1/agents/{AGENT}/configs", json=_config_body(1))
    assert r.status_code == 201
    assert r.json()["version"] == 1
    assert r.json()["prompt"]["version"] == 1

    r = client.get(f"/v1/agents/{AGENT}/configs/1")
    assert r.status_code == 200
    assert r.json()["tools"]["version"] == 5


def test_full_lifecycle_over_http(client):
    """创建 -> 部署 -> resolve -> 灰度 -> 回滚，全走 HTTP。"""
    client.post("/v1/prompts", json={"name": AGENT})
    client.post(f"/v1/prompts/{AGENT}/versions", json=_prompt_version_body("简洁"))
    client.post(f"/v1/prompts/{AGENT}/versions", json=_prompt_version_body("详细"))
    client.post(f"/v1/agents/{AGENT}/configs", json=_config_body(1))
    client.post(f"/v1/agents/{AGENT}/configs", json=_config_body(2))

    # 部署 v1 到 prod
    r = client.post("/v1/deployments", json={
        "agent": AGENT, "environment": "prod", "version": 1, "created_by": "alice",
    })
    assert r.status_code == 201
    dep_id = r.json()["id"]
    assert r.json()["status"] == "RELEASED"

    # canary v2 10%
    r = client.post("/v1/deployments", json={
        "agent": AGENT, "environment": "prod", "version": 2, "traffic_percent": 10,
        "experiment": "v2_test", "created_by": "alice", "reason": "ab",
    })
    assert r.json()["status"] == "CANARY"

    # resolve
    r = client.get(f"/v1/resolve?agent={AGENT}&environment=prod&user_id=user_123")
    assert r.status_code == 200
    data = r.json()
    assert data["config_version"] in (1, 2)
    assert data["routing"]["experiment"] == "v2_test"
    assert data["routing"]["variant"] in ("A", "B", "single")
    assert "template" in data["prompt"]

    # 灰度到 100%
    r = client.post(f"/v1/deployments/{dep_id}/rollout", json={
        "version": 2, "traffic_percent": 100, "created_by": "alice",
    })
    assert r.json()["status"] == "RELEASED"

    # 回滚
    r = client.post(f"/v1/deployments/{dep_id}/rollback", json={
        "created_by": "ops", "reason": "tool error up",
    })
    assert r.status_code == 200
    assert {x["version"] for x in r.json()["rules"]} == {1}

    # 审计里有全过程
    r = client.get("/v1/audit")
    actions = [e["action"] for e in r.json()]
    assert "DEPLOY" in actions and "ROLLOUT" in actions and "ROLLBACK" in actions


def test_error_mapping_over_http(client):
    # 部署不存在的 config -> 404
    r = client.post("/v1/deployments", json={
        "agent": AGENT, "environment": "prod", "version": 99,
    })
    assert r.status_code == 404

    # resolve 未部署的 agent -> 404
    r = client.get("/v1/resolve?agent=nope&environment=prod&user_id=u")
    assert r.status_code == 404

    # 重复创建 prompt -> 409
    client.post("/v1/prompts", json={"name": AGENT})
    r = client.post("/v1/prompts", json={"name": AGENT})
    assert r.status_code == 409

    # 非法流量 -> 400
    client.post(f"/v1/prompts/{AGENT}/versions", json=_prompt_version_body("x"))
    client.post(f"/v1/agents/{AGENT}/configs", json=_config_body(1))
    r = client.post("/v1/deployments", json={
        "agent": AGENT, "environment": "prod", "version": 1, "traffic_percent": 150,
    })
    assert r.status_code == 422  # pydantic 范围校验


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
