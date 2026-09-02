"""一键验收脚本结果的回归测试（设计说明书 §37 八个实验）。"""
from __future__ import annotations

import pytest

from app.factory import build_guardrails

from experiments.run_experiments import run_experiments


@pytest.fixture
def g():
    return build_guardrails()


async def test_eight_demos_all_pass(g):
    results = await run_experiments(g)
    assert len(results) == 8
    assert all(results.values()), f"存在失败的实验: {[k for k, v in results.items() if not v]}"
