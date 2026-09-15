"""Decay/Forget + Wake：艾宾浩斯代谢、画像豁免、高访问抗衰、可唤醒。"""
from __future__ import annotations

import pytest

from memory_engine.engine import MemoryEngine
from memory_engine.records import EPISODIC, PROFILE


def _idle_for(engine: MemoryEngine, rec, days: float):
    """把一条记忆“晾”上 days 天(等价 simulate_elapsed 只影响该条)。"""
    from datetime import timedelta
    rec.last_accessed_at = rec.last_accessed_at - timedelta(days=days)
    return rec


def test_episodic_low_vitality_sleeps(engine):
    rec = engine.remember("alice", "5G 基站闪断", "备查", "常年无人问津的旧经验", EPISODIC, confidence=0.7)
    _idle_for(engine, rec, days=120)  # 半衰期 30 天 → 过了 4 个半衰期
    slept = engine.run_decay_cycle(half_life_days=30.0, retention_floor=0.2)
    assert any(r.memory_id == rec.memory_id for r in slept)
    assert rec.is_active is False
    assert rec.valid_to is not None


def test_profile_never_decays_even_when_idle_forever(engine):
    rec = engine.remember("alice", "用户画像", "语言", "只讲中文", PROFILE)
    _idle_for(engine, rec, days=3650)
    slept = engine.run_decay_cycle(half_life_days=30.0, retention_floor=0.0)
    assert all(r.memory_id != rec.memory_id for r in slept)
    assert rec.is_active is True


def test_heavily_accessed_memory_survives_decay(engine):
    # access_count=20 的记忆即便闲置很久仍因 (1+ln(1+20)) 的乘数而保住
    rec = engine.remember("alice", "高频经验", "排障", "每次都被召回的常用经验", EPISODIC)
    rec.access_count = 20
    _idle_for(engine, rec, days=90)
    slept = engine.run_decay_cycle(half_life_days=30.0, retention_floor=0.2)
    assert all(r.memory_id != rec.memory_id for r in slept)
    assert rec.is_active is True


def test_decay_returns_all_slept_in_one_cycle(engine):
    a = engine.remember("alice", "A", "经验", "闲置经验甲", EPISODIC, confidence=0.5)
    b = engine.remember("alice", "B", "经验", "闲置经验乙", EPISODIC, confidence=0.5)
    for r in (a, b):
        _idle_for(engine, r, days=200)
    slept = engine.run_decay_cycle()
    assert {r.memory_id for r in slept} == {a.memory_id, b.memory_id}


def test_decay_skips_already_inactive(engine):
    rec = engine.remember("alice", "A", "经验", "已被证伪的记录", EPISODIC)
    engine.delete(rec.memory_id, hard_delete=False)
    _idle_for(engine, rec, days=500)
    slept = engine.run_decay_cycle(half_life_days=30.0, retention_floor=0.0)
    assert all(r.memory_id != rec.memory_id for r in slept)


def test_wake_after_decay_gives_new_lifecycle(engine):
    rec = engine.remember("alice", "5G 基站闪断", "备查", "沉睡后还能被唤醒", EPISODIC)
    _idle_for(engine, rec, days=365)
    engine.run_decay_cycle()          # 入睡
    assert rec.is_active is False
    woken = engine.wake_reactivate(rec.memory_id)
    assert woken is not None
    assert woken.is_active is True
    assert woken.confidence == pytest.approx(0.6)
    assert woken.valid_to is None


def test_decay_worker_run_once_accumulates_sweeps_and_reports(engine):
    from memory_engine.config import AgentMemoryConfig
    from memory_engine.worker.decay import DecayWorker
    w = DecayWorker(engine, AgentMemoryConfig(seed_demo=False, half_life_days=30.0))
    assert w.sweeps == 0
    w.run_once()
    assert w.sweeps == 1
    w.run_once()
    assert w.sweeps == 2
