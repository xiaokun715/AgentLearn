"""Retrieve：混合打分排序 / 状态隔离 / subject 过滤 / 阈值 / 深潜检索。"""
from __future__ import annotations

from datetime import timedelta

from memory_engine.engine import MemoryEngine
from memory_engine.records import EPISODIC, SEMANTIC


def _seed_two_related(eng: MemoryEngine):
    """同主题两条 + 一条无关 + 一个他人主题，用于打分排序测试。"""
    eng.remember("alice", "5G 基站闪断", "排障", "先查小区退服计数, 再看切换失败率与上行干扰",
                 EPISODIC, confidence=0.9)
    eng.remember("alice", "5G 基站闪断", "备查", "同一小区闪断建议直接提交换板流程",
                 EPISODIC, confidence=0.6)
    eng.remember("alice", "Java 排序", "经验", "用归并排序写一个算法题", SEMANTIC, confidence=1.0)
    eng.remember("bob", "5G 基站闪断", "排障", "B 用户的相似经验", EPISODIC, confidence=1.0)


def test_retrieve_ranks_by_score_and_respects_top_k(engine):
    _seed_two_related(engine)
    hits = engine.retrieve("alice", engine.embedder.embed("另一个 5G 基站闪断也报障了怎么查"),
                           top_k=2, sim_threshold=0.30)
    assert len(hits) == 2
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)  # 按 rank_score 倒序
    assert all(r.subject == "5G 基站闪断" for r, _ in hits)  # 无关主题被压掉
    assert all(r.user_id == "alice" for r, _ in hits)


def test_retrieve_excludes_inactive_and_other_user(engine):
    _seed_two_related(engine)
    victim = engine.remember("alice", "5G 基站闪断", "排障", "这条已被证伪弃用", EPISODIC)
    engine.delete(victim.memory_id, hard_delete=False)
    hits = engine.retrieve("alice", engine.embedder.embed("5G 基站闪断 排查"),
                           top_k=10, sim_threshold=0.0)
    ids = [r.memory_id for r, _ in hits]
    assert victim.memory_id not in ids
    assert all(r.user_id == "alice" for r, _ in hits)  # bob 的记忆进不来


def test_subject_filter(engine):
    _seed_two_related(engine)
    hits = engine.retrieve("alice", engine.embedder.embed("怎么写一个 Java 排序算法"),
                           subject_filter="Java 排序", top_k=5, sim_threshold=0.0)
    assert hits and hits[0][0].subject == "Java 排序"
    assert len(hits) == 1


def test_similarity_threshold_gates_weak_matches(engine):
    engine.remember("alice", "天气", "闲聊", "今天上海晴到多云适合出门", EPISODIC)
    hits = engine.retrieve("alice", engine.embedder.embed("如何排查基站闪断"),
                           top_k=5, sim_threshold=0.99)  # 极高阈值 → 空
    assert hits == []


def test_deep_retrieve_includes_sleeping_cold_memories(engine):
    cold = engine.remember("alice", "5G 基站闪断", "备查", "旧经验: 曾经直接换板解决", EPISODIC)
    engine.delete(cold.memory_id, hard_delete=False)  # 让它沉睡
    # 普通 retrieve 搜不到
    assert engine.retrieve("alice", engine.embedder.embed("5G 基站闪断"), top_k=10,
                           sim_threshold=0.0) == []
    # 深潜检索能捞到沉睡记忆
    deep = engine.deep_retrieve("alice", engine.embedder.embed("5G 基站闪断"), top_k=10)
    sleeping = [r for r, _ in deep if not r.is_active]
    assert any(r.memory_id == cold.memory_id for r in sleeping)


def test_retrieve_after_touch_boosts_lifecycle(engine):
    """被 touch 强化的记忆 time_decay 项更高(同内容一旧一新时新的排前面)。"""
    eng = engine
    old = eng.remember("alice", "告警A", "排障", "先查指标再抓信令 的完整句子文本", EPISODIC)
    eng.simulate_elapsed(days=5)  # 变旧
    fresh = eng.remember("alice", "告警A", "排障", "先查指标再抓信令 的完整句子文本", EPISODIC)
    hits = eng.retrieve("alice", eng.embedder.embed("先查指标再抓信令 的完整句子文本"),
                        top_k=2, sim_threshold=0.30)
    # 时间衰减让 fresh 排在 old 前面(仅当排序区分开了)
    assert hits[0][0].memory_id == fresh.memory_id
