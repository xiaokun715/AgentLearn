"""引擎 8 原语各自的行为(说明书 §核心操作)：Insert / SCD2 / Delete / Consolidate /
Touch / Decay(另见 test_decay) / Wake / 糖方法。"""
from __future__ import annotations

import pytest

from memory_engine.engine import MemoryEngine
from memory_engine.records import EPISODIC, PROFILE, SEMANTIC, utcnow


def _q(eng: MemoryEngine, text: str) -> list[float]:
    return eng.embedder.embed(text)


def _add(eng: MemoryEngine, subject="5G 基站闪断", value="先查退服计数再看切换失败率",
         mtype=EPISODIC, conf=1.0):
    return eng.remember("alice", subject, "排障经验", value, mtype, confidence=conf)


# ---------------------------------------------------------------------
# 1. Insert
# ---------------------------------------------------------------------
def test_insert_stores_active_record(engine):
    rec = engine.insert("alice", "告警", "原因", "上行干扰", SEMANTIC,
                        embedding=_q(engine, "上行干扰"))
    assert rec.memory_id in engine.storage
    assert rec.memory_id.startswith("mem_")
    assert rec.is_active and rec.version == 1 and rec.confidence == 1.0
    assert engine.get(rec.memory_id) is rec


def test_insert_auto_embeds_via_doc(engine):
    rec = engine.remember("alice", "基带告警", "排障", "先抓 S1 信令", EPISODIC)
    assert rec.embedding  # 自动编码非空


def test_insert_generates_unique_ids(engine):
    ids = {engine.remember("u", "s", "p", f"v{i}").memory_id for i in range(50)}
    assert len(ids) == 50


# ---------------------------------------------------------------------
# 2. Update / Supersede (SCD2)
# ---------------------------------------------------------------------
def test_supersede_archives_old_and_bumps_version(engine):
    old = _add(engine)
    new = engine.update_supersede(old.memory_id, "新增：先看 S1 切换准备消息")
    assert old.is_active is False and old.valid_to is not None
    assert new.memory_id != old.memory_id
    assert new.version == old.version + 1
    assert new.is_active is True
    assert new.user_id == old.user_id and new.subject == old.subject


def test_history_returns_version_lineage(engine):
    v1 = _add(engine)
    v2 = engine.update_supersede(v1.memory_id, "第二代做法")
    v3 = engine.update_supersede(v2.memory_id, "第三代做法")
    lineage = engine.history("alice", "5G 基站闪断")
    assert [r.version for r in lineage] == [1, 2, 3]
    assert lineage[-1].is_active is True


def test_supersede_missing_id_raises(engine):
    with pytest.raises(KeyError):
        engine.update_supersede("mem_nope", "x")


# ---------------------------------------------------------------------
# 3. Delete
# ---------------------------------------------------------------------
def test_soft_delete_keeps_record_inactive(engine):
    rec = _add(engine)
    engine.delete(rec.memory_id, hard_delete=False)
    assert rec.memory_id in engine.storage
    assert rec.is_active is False and rec.valid_to is not None


def test_hard_delete_removes_physically(engine):
    rec = _add(engine)
    engine.delete(rec.memory_id, hard_delete=True)
    assert rec.memory_id not in engine.storage


def test_delete_unknown_is_noop(engine):
    engine.delete("mem_ghost")  # 不应抛异常


# ---------------------------------------------------------------------
# 4. Consolidate
# ---------------------------------------------------------------------
def test_consolidate_promotes_to_semantic_sop(engine):
    frags = [_add(engine, value=t) for t in ["场景A 退服高", "场景B 切换失败", "场景C 邻区漏配"]]
    sop = engine.consolidate([f.memory_id for f in frags], "5G 基站闪断三步走")
    assert sop.memory_type == SEMANTIC
    assert sop.predicate == "consolidated_sop"
    assert all(not f.is_active for f in frags)  # 碎片全部软下线
    # 置信度 = min(1, avg+0.1)
    assert sop.confidence == pytest.approx(min(1.0, 1.0 + 0.1))


def test_consolidate_bad_inputs(engine):
    with pytest.raises(ValueError):
        engine.consolidate(["mem_unknown"], "x")
    with pytest.raises(ValueError):
        engine.consolidate([], "x")
    # 跨用户不可合并
    a = _add(engine)
    b = engine.remember("bob", "告警", "经验", "别的用户", EPISODIC)
    with pytest.raises(ValueError):
        engine.consolidate([a.memory_id, b.memory_id], "x")


# ---------------------------------------------------------------------
# 5. Touch
# ---------------------------------------------------------------------
def test_touch_success_strengthens(engine):
    rec = _add(engine, conf=0.8)
    engine.touch_reinforce(rec.memory_id, task_succeeded=True)
    r = engine.get(rec.memory_id)
    assert r.access_count == 1
    assert r.confidence == pytest.approx(0.85)
    assert r.last_accessed_at >= r.created_at


def test_touch_success_caps_at_one(engine):
    rec = _add(engine, conf=0.99)
    engine.touch_reinforce(rec.memory_id, task_succeeded=True)
    assert engine.get(rec.memory_id).confidence == pytest.approx(1.0)


def test_touch_failure_weakens_then_auto_deactivates(engine):
    rec = _add(engine)  # conf 1.0
    for _ in range(3):  # 1.0->.75->.50->.25
        engine.touch_reinforce(rec.memory_id, task_succeeded=False)
    r = engine.get(rec.memory_id)
    assert r.confidence == pytest.approx(0.25)
    assert r.is_active is False  # conf<0.35 → 自动失活


# ---------------------------------------------------------------------
# 7. Wake (Decay 单独在 test_decay)
# ---------------------------------------------------------------------
def test_wake_reactivates_with_mid_confidence(engine):
    rec = _add(engine)
    engine.delete(rec.memory_id, hard_delete=False)  # 先失活
    woken = engine.wake_reactivate(rec.memory_id)
    assert woken is not None
    assert woken.is_active is True
    assert woken.valid_to is None
    assert woken.confidence == pytest.approx(0.6)


def test_wake_unknown_returns_none(engine):
    assert engine.wake_reactivate("mem_nope") is None


# ---------------------------------------------------------------------
# 糖方法 & 时间快进
# ---------------------------------------------------------------------
def test_simulate_elapsed_moves_clock_back(engine):
    rec = _add(engine)
    before = rec.last_accessed_at
    engine.simulate_elapsed(days=10)
    from datetime import timedelta
    assert (before - rec.last_accessed_at) >= timedelta(days=9.99)


def test_stats_shape(engine):
    _add(engine)
    _add(engine, subject="其它", value="无关")
    engine.remember("alice", "画像", "语言", "中文", PROFILE)
    stats = engine.stats()
    assert stats["total"] == 3 and stats["active"] == 3 and stats["sleeping"] == 0
    assert stats["by_type"]["EPISODIC"] == 2 and stats["by_type"]["PROFILE"] == 1


def test_active_and_sleeping_views(engine):
    rec = _add(engine)
    engine.delete(rec.memory_id)
    assert rec.memory_id in [r.memory_id for r in engine.sleeping_memories()]
    assert rec.memory_id not in [r.memory_id for r in engine.active_memories()]
