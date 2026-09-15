"""四层记忆：Working 预算淘汰 / Short-Term 折叠+Checkpoint /
Episodic 存取 / Semantic+Profile 点查 / system 热注水。"""
from __future__ import annotations

from memory_engine.tiers.shortterm import ShortTermMemory
from memory_engine.tiers.working import WorkingMemory, approx_tokens


# ---------------------------------------------------------------------
# Working Memory
# ---------------------------------------------------------------------
def test_working_budget_evicts_oldest_keeps_pinned_and_newest():
    wm = WorkingMemory(budget_units=40)
    wm.add("profile", "语言: 中文", source="ProfileStore")          # protected
    wm.add("episodic", "回忆: NR 切换失败 | 先看测量报告", source="EpisodicRecall")
    before_kinds = [f.kind for f in wm.frames]
    assert before_kinds == ["profile", "episodic"]
    # 塞入一个超大的工具返回(严格入模过滤)
    wm.add("tool", "retry=timeout " * 60)
    kinds = [f.kind for f in wm.frames]
    assert "profile" in kinds            # 钉住的画像不被裁
    assert "tool" in kinds               # 最新一帧(刚产出)不丢
    assert "episodic" not in kinds       # 最旧的非保护帧被淘汰
    assert any(f.kind == "episodic" for f in wm.trimmed)


def test_working_used_units_and_budget_bookkeeping():
    wm = WorkingMemory(budget_units=1000)
    wm.add("turn", "你好" * 50)
    assert wm.used_units() > 0 and wm.used_units() <= 1000
    wm.clear()
    assert wm.used_units() == 0 and len(wm.frames) == 0


def test_assemble_context_renders_frames_and_dedupes():
    wm = WorkingMemory(budget_units=500)
    wm.add("system", "你是网优 Agent")
    wm.add("profile", "语言: 中文")
    wm.add("profile", "语言: 中文")   # 相同帧重复 → assemble 去重
    text = wm.assemble_context()
    assert text.count("语言: 中文") == 1
    assert "你是网优 Agent" in text


def test_approx_tokens_smoke():
    assert approx_tokens("") == 0
    assert approx_tokens("中文内容") >= 1
    assert approx_tokens("a" * 40) >= 1


# ---------------------------------------------------------------------
# Short-Term Memory
# ---------------------------------------------------------------------
def test_shortterm_folds_old_turns_keeps_recent():
    st = ShortTermMemory(window=6, keep_recent=4)
    for i in range(20):
        st.append("user", f"第{i}轮: NR 切换失败怎么定位")
        st.append("assistant", f"第{i}轮回复: 先看测量报告")
    # 近轮在窗口内, 折叠摘要非空
    assert len(st.turns) <= 6
    assert len(st.summary) > 0


def test_shortterm_checkpoint_roundtrip():
    st = ShortTermMemory(window=8, keep_recent=4)
    st.append("user", "请排查 VoLTE 掉话")
    st.note("target", "VoLTE")
    snap = st.checkpoint()
    st2 = ShortTermMemory(window=8, keep_recent=4)
    st2.restore(snap)
    assert st2.scratchpad == {"target": "VoLTE"}
    assert [t.content for t in st2.turns] == [t.content for t in st.turns]
    assert st2.summary == st.summary


def test_shortterm_scratchpad_notes():
    st = ShortTermMemory()
    st.note("phase", "completed")
    assert st.checkpoint()["scratchpad"]["phase"] == "completed"


# ---------------------------------------------------------------------
# Episodic Memory (Top-K RAG)
# ---------------------------------------------------------------------
def test_episodic_save_and_recall(rt_clean):
    epi = rt_clean.episodic
    epi.save_fact("alice", "5G 基站闪断", "排障经验",
                  "先查小区退服计数, 再看切换失败率与上行干扰", confidence=0.9)
    hits = epi.recall("另一个 5G 基站闪断怎么处理", "alice", top_k=3)
    assert len(hits) == 1 and hits[0][0].subject == "5G 基站闪断"
    assert epi.few_shots("5G 基站闪断", "alice")  # 生成 few-shot 行


def test_episodic_user_isolation(rt_clean):
    rt_clean.episodic.save_fact("alice", "告警A", "经验",
                                "处理告警A：先抓 S1 信令定位根因, 再核查上行干扰指标")
    rt_clean.episodic.save_fact("bob", "告警A", "经验",
                                "处理告警A：鲍勃习惯直接重启小区看是否恢复")
    # 各自只能召回自己名下那一条(引擎做 user 门禁隔离)
    assert [r.subject for r, _ in rt_clean.episodic.recall("告警A 怎么处理", "alice")] == ["告警A"]
    assert [r.subject for r, _ in rt_clean.episodic.recall("告警A 怎么处理", "bob")] == ["告警A"]
    assert rt_clean.episodic.recall("告警A 怎么处理", "carol") == []  # 陌生人搜不到任何经验


# ---------------------------------------------------------------------
# Semantic + Profile
# ---------------------------------------------------------------------
def test_profile_store_point_load(rt_clean):
    rt_clean.profile.set_many("alice", {"语言": "中文", "集群": "华东2"})
    assert rt_clean.profile.get("alice") == {"语言": "中文", "集群": "华东2"}
    assert rt_clean.profile.as_lines("alice") == ["语言: 中文", "集群: 华东2"]
    assert rt_clean.profile.get("nobody") == {}


def test_semantic_point_query_and_rule(rt_clean):
    sm = rt_clean.semantic
    sm.save_rule("alice", "时延优化", "先查传输抖动再查空口", predicate="规则")
    rules = sm.point_query("alice", "时延优化")
    assert len(rules) == 1 and rules[0].memory_type == "SEMANTIC"
    assert sm.point_query("alice", "不存在主题") == []
    # subject 子串命中式点查
    assert len(sm.point_query_by_text("alice", "时延优化怎么做")) == 1


# ---------------------------------------------------------------------
# AgentMemorySystem hydration
# ---------------------------------------------------------------------
def test_hydration_brings_profile_recall_and_rules(rt_seed):
    rt = rt_seed
    out = rt.system.hydrate("alice", "排查 NR 切换失败怎么处理", "你是网优 Agent")
    assert out["query"].startswith("排查 NR")
    assert any("华东2" in line for line in out["profile"])
    assert len(out["recalled"]) >= 1                 # 语义召回种子经验
    assert any("你是网优 Agent" in line for line in out["context"].splitlines())
    # 工作记忆里有画像帧
    assert any(f["kind"] == "profile" for f in out["working"]["frames"])


def test_tiers_snapshot_shape(rt_seed):
    snap = rt_seed.system.tiers_snapshot("alice")
    assert {"working", "shortterm", "episodic_counts", "profile",
            "semantic_rules", "engine_stats"} <= set(snap)
    assert "语言" in snap["profile"]          # 画像静态字段在快照里


def test_run_canned_session_three_stages(rt_seed):
    stages = rt_seed.system.run_canned_session()
    assert len(stages) == 3
    # 蒸馏后引擎里新增了 EPISODIC / SEMANTIC 记忆
    assert rt_seed.engine.stats()["total"] > 5
