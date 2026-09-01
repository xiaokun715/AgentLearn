"""Prompt Registry 测试：版本不可变、自动递增、变量/元数据（设计说明书 §7~§8）。"""
from __future__ import annotations

import pytest

from app.domain.exceptions import ConflictError, NotFoundError


async def test_prompt_version_auto_increment(seeded):
    """v1 -> v2 自动递增。"""
    assert seeded["prompt_v1"] == 1
    assert seeded["prompt_v2"] == 2


async def test_versions_are_immutable(runtime, seeded):
    """版本创建后不可修改：重复创建同版本报错，v1 内容未被篡改。"""
    pr = runtime.prompt_registry
    # 试图创建 v3，版本号自动前进 —— 而不是覆盖 v1/v2
    pv3 = await pr.create_version("test_case_agent", template="v3", created_by="tester")
    assert pv3.version == 3
    # v1 原封不动
    assert (await pr.require_version("test_case_agent", 1)).template == "请简洁回答问题。"


async def test_version_conflict_on_duplicate(runtime, seeded):
    """即使强制指定重复版本，存储层也会拒绝（UNIQUE(prompt, version)）。"""
    pr = runtime.prompt_registry
    with pytest.raises(ConflictError):
        # 直接走 repo 强制写入同版本（正常 API 不会传 version）
        await pr._repo.add_prompt_version(
            __import__("app.domain.prompt", fromlist=["PromptVersion"]).PromptVersion(
                id="dup-id", prompt_name="test_case_agent", version=1,
                template="hijack", created_by="x",
            )
        )


async def test_version_variables_and_metadata_preserved(runtime, seeded):
    pv = await runtime.prompt_registry.require_version("test_case_agent", 1)
    assert pv.variables == ["question"]
    assert pv.metadata == {}


async def test_require_missing_version_raises(runtime):
    await runtime.prompt_registry.create_prompt("ghost", created_by="t")
    with pytest.raises(NotFoundError):
        await runtime.prompt_registry.require_version("ghost", 99)


async def test_create_version_requires_prompt(runtime):
    """对不存在的 Prompt 追加版本应报错。"""
    with pytest.raises(NotFoundError):
        await runtime.prompt_registry.create_version(
            "no_such_prompt", template="x", created_by="t"
        )


async def test_list_versions_ordered(runtime, seeded):
    versions = await runtime.prompt_registry.list_versions("test_case_agent")
    assert [v.version for v in versions] == [1, 2]
