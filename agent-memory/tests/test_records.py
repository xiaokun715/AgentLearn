"""MemoryRecord 数据模型：默认值 / 校验 / 序列化往返。"""
from __future__ import annotations

from datetime import timezone

import pytest

from memory_engine.records import (
    EPISODIC,
    PROFILE,
    SEMANTIC,
    MemoryRecord,
    utcnow,
)


def test_defaults_look_like_spec():
    rec = MemoryRecord(memory_id="m1", user_id="alice", subject="S", predicate="P", object_value="O")
    assert rec.memory_type == SEMANTIC
    assert rec.confidence == 1.0
    assert rec.version == 1
    assert rec.is_active is True
    assert rec.access_count == 0
    assert rec.valid_to is None
    # 时间必须是带时区的(衰减计算需要 aware datetime)
    assert rec.created_at.tzinfo is not None
    assert rec.created_at.tzinfo == timezone.utc


@pytest.mark.parametrize("bad_type", ["FOO", "LONG_TERM", "profile "])
def test_invalid_memory_type_rejected(bad_type):
    with pytest.raises(ValueError):
        MemoryRecord(memory_id="m", user_id="u", subject="s", predicate="p",
                     object_value="o", memory_type=bad_type)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_conf", [-0.1, 1.1, 7])
def test_confidence_out_of_range_rejected(bad_conf):
    with pytest.raises(ValueError):
        MemoryRecord(memory_id="m", user_id="u", subject="s", predicate="p",
                     object_value="o", confidence=bad_conf)


def test_negative_access_count_rejected():
    with pytest.raises(ValueError):
        MemoryRecord(memory_id="m", user_id="u", subject="s", predicate="p",
                     object_value="o", access_count=-3)


def test_to_dict_from_dict_roundtrip():
    rec = MemoryRecord(memory_id="m9", user_id="alice", subject="告警", predicate="排障",
                       object_value="先查话统", memory_type=EPISODIC, confidence=0.8,
                       embedding=[0.1, 0.2], version=2, is_active=False)
    rec.valid_to = utcnow()
    restored = MemoryRecord.from_dict(rec.to_dict())
    assert restored == rec
    # JSON 友好：时间已是字符串
    raw = rec.to_dict()
    assert isinstance(raw["created_at"], str)
    assert isinstance(raw["valid_to"], str)


def test_types_constants():
    assert (PROFILE, EPISODIC, SEMANTIC) == ("PROFILE", "EPISODIC", "SEMANTIC")


def test_snapshot_human_readable():
    rec = MemoryRecord(memory_id="m1", user_id="alice", subject="S", predicate="P",
                       object_value="O", memory_type=PROFILE, is_active=False)
    text = rec.snapshot()
    assert rec.memory_id in text
    assert "PROFILE" in text and "○沉睡" in text
