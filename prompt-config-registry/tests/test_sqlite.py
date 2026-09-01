"""SQLite 后端集成测试 —— 验证持久化路径与内存后端语义一致。"""
from __future__ import annotations

import pytest

from app.config import RegistryConfig
from app.factory import Runtime, build_runtime


@pytest.fixture
async def sqlite_runtime(tmp_path) -> Runtime:
    rt = await build_runtime(
        RegistryConfig(
            storage_backend="sqlite",
            database_url=f"sqlite:///{tmp_path}/registry.db",
            cache_backend="memory",
        )
    )
    yield rt
    await rt.stop()


async def test_sqlite_full_lifecycle(sqlite_runtime):
    rt = sqlite_runtime
    pr, cr, pub, rsv = rt.prompt_registry, rt.config_registry, rt.publisher, rt.resolver

    await pr.create_prompt("test_case_agent", created_by="t")
    p1 = await pr.create_version("test_case_agent", template="简洁", created_by="t")
    p2 = await pr.create_version("test_case_agent", template="详细", created_by="t")
    c1 = await cr.create_config(
        "test_case_agent", prompt={"name": "test_case_agent", "version": p1.version}, created_by="t"
    )
    c2 = await cr.create_config(
        "test_case_agent", prompt={"name": "test_case_agent", "version": p2.version}, created_by="t"
    )

    await pub.publish("test_case_agent", "prod", c1.version, created_by="t")
    dep = await pub.publish(
        "test_case_agent", "prod", c2.version, traffic_percent=10, created_by="t"
    )
    assert dep.status == "CANARY"

    snap = await rsv.resolve("test_case_agent", "prod", "user_1")
    assert snap.config_version in (c1.version, c2.version)

    rolled = await rt.rollback_service.rollback(dep.id, created_by="t")
    assert {r.version: r.weight for r in rolled.rules} == {c1.version: 100}

    # 审计落库
    assert len(await rt.audit_service.list()) >= 6


async def test_sqlite_persists_between_restarts(tmp_path):
    """SQLite 是持久化的：重启后数据还在（文件即库）。"""
    url = f"sqlite:///{tmp_path}/registry.db"

    rt1 = await build_runtime(RegistryConfig(storage_backend="sqlite", database_url=url))
    await rt1.prompt_registry.create_prompt("persist_agent", created_by="t")
    await rt1.prompt_registry.create_version("persist_agent", template="hello", created_by="t")
    await rt1.stop()

    rt2 = await build_runtime(RegistryConfig(storage_backend="sqlite", database_url=url))
    try:
        pv = await rt2.prompt_registry.require_version("persist_agent", 1)
        assert pv.template == "hello"
    finally:
        await rt2.stop()


async def test_sqlite_immutability(sqlite_runtime):
    """SQLite 唯一键同样拦截重复版本。"""
    pr = sqlite_runtime.prompt_registry
    await pr.create_prompt("a", created_by="t")
    await pr.create_version("a", template="v1", created_by="t")
    from app.domain.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await pr._repo.add_prompt_version(
            __import__("app.domain.prompt", fromlist=["PromptVersion"]).PromptVersion(
                id="dup", prompt_name="a", version=1, template="hijack", created_by="x"
            )
        )
