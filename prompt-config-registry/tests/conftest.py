"""共享测试 fixtures —— 默认内存后端，快且零依赖。"""
from __future__ import annotations

import pytest

from app.config import RegistryConfig
from app.factory import Runtime, build_runtime


@pytest.fixture
async def runtime() -> Runtime:
    rt = await build_runtime(
        RegistryConfig(storage_backend="memory", cache_backend="memory")
    )
    yield rt
    await rt.stop()


async def _seed(runtime: Runtime, *, agent: str = "test_case_agent") -> dict:
    """造一组标准数据：prompt v1/v2、config v1/v2，返回引用。

    返回: {prompt_v1, prompt_v2, config_v1, config_v2}
    """
    pr, cr = runtime.prompt_registry, runtime.config_registry
    await pr.create_prompt(agent, created_by="tester")
    pv1 = await pr.create_version(
        agent, template="请简洁回答问题。", variables=["question"], created_by="tester"
    )
    pv2 = await pr.create_version(
        agent, template="请详细回答问题，并给出三个例子。", created_by="tester"
    )
    c1 = await cr.create_config(
        agent,
        prompt={"name": agent, "version": pv1.version},
        tools={"version": 3},
        created_by="tester",
    )
    c2 = await cr.create_config(
        agent,
        prompt={"name": agent, "version": pv2.version},
        tools={"version": 5},
        created_by="tester",
    )
    return {
        "prompt_v1": pv1.version,
        "prompt_v2": pv2.version,
        "config_v1": c1.version,
        "config_v2": c2.version,
    }


@pytest.fixture
async def seeded(runtime) -> dict:
    """返回已 seed 的版本号引用。"""
    return await _seed(runtime)
