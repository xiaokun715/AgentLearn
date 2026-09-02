"""Toxicity Detector 测试（§9 可扩展类别之一）。"""
from __future__ import annotations


async def test_toxic_input_blocked(g):
    r = await g.check_input("你真是个白痴吗？")
    assert r.blocked is True
    assert "TOXICITY" in {f.category for f in r.findings}


async def test_toxic_output_blocked(g):
    r = await g.check_output("你这个蠢货，答案全错了")
    assert r.blocked is True
    assert "TOXICITY" in {f.category for f in r.findings}


async def test_clean_passes(g):
    assert (await g.check_input("请问 5G 覆盖优化怎么做？")).action.value == "allow"
