"""Config Registry 测试（设计说明书 §9~§11）。"""
from __future__ import annotations

import pytest

from app.domain.exceptions import ConflictError, NotFoundError


async def test_config_version_auto_increment(seeded):
    assert seeded["config_v1"] == 1
    assert seeded["config_v2"] == 2


async def test_config_is_a_snapshot(runtime, seeded):
    """Config 存的是引用组合快照：model / parameters / prompt / tools / guardrails。"""
    cfg = await runtime.config_registry.require_config("test_case_agent", seeded["config_v2"])
    d = cfg.to_dict()
    assert d["model"]["provider"] == "qwen"
    assert d["model"]["name"] == "qwen3.5-27b"
    assert d["parameters"]["temperature"] == 0.2
    assert d["prompt"] == {"name": "test_case_agent", "version": seeded["prompt_v2"]}
    assert d["tools"]["version"] == 5


async def test_config_rejects_missing_prompt_version(runtime):
    """Config 引用不存在的 Prompt 版本 -> 报错（防止发布指向空气的配置）。"""
    with pytest.raises(NotFoundError):
        await runtime.config_registry.create_config(
            "test_case_agent",
            prompt={"name": "test_case_agent", "version": 999},
            created_by="t",
        )


async def test_config_is_immutable(runtime, seeded):
    """Config 版本不可变：重复版本号冲突。"""
    cr = runtime.config_registry
    with pytest.raises(ConflictError):
        await cr._repo.create_config(
            __import__("app.domain.config", fromlist=["AgentConfig"]).AgentConfig(
                agent_name="test_case_agent",
                version=seeded["config_v1"],
                prompt=__import__(
                    "app.domain.config", fromlist=["PromptRef"]
                ).PromptRef("test_case_agent", 1),
            )
        )


async def test_list_configs_ordered(runtime, seeded):
    configs = await runtime.config_registry.list_configs("test_case_agent")
    assert [c.version for c in configs] == [1, 2]
