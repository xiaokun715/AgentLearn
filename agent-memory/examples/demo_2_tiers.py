"""demo_2：四层记忆 + 热注水 —— 一个请求进来，模型“实际看到”什么。

运行：``python examples/demo_2_tiers.py``

覆盖说明书 §分层记忆 与 §热注水：
* Profile(第4层·画像) 全量点查静态加载 → 进工作记忆
* Episodic(第3层) Top-K 语义召回(few-shot) → 进工作记忆
* Semantic(第4层) subject 点查规则 → 进工作记忆
* Short-Term(第2层) 会话折叠 + Checkpointer 快照/恢复
* Working(第1层) 有预算的上下文窗口 —— 塞入超大工具返回会被“严格入模过滤”
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from memory_engine.config import AgentMemoryConfig
from memory_engine.factory import build_runtime

from _lib import banner, enable_utf8, section, show

USER = "alice"
SYSTEM = "你是通信网优工程师 Agent：回答前先回忆历史排障经验，再结合画像偏好组织答复。"
QUERY = "排查 NR 切换失败怎么处理"


def main() -> int:
    enable_utf8()
    rt = build_runtime(AgentMemoryConfig(seed_demo=True))

    banner("DEMO 2 · 四层记忆金字塔 + 一次请求的热注水(Hydration)")

    section("第 1 步 · 四层记忆当前概况(tiers snapshot)")
    snap = rt.system.tiers_snapshot(USER)
    show([
        f"Profile(第4层·画像) : {snap['profile']}",
        f"Semantic(第4层·规则): {len(snap['semantic_rules'])} 条",
        f"Episodic(第3层·经验): {snap['episodic_counts'].get('EPISODIC', 0)} 条",
        f"ShortTerm(第2层)    : {snap['shortterm']['turns_count']} 轮已记录, 窗口={snap['shortterm']['window']}",
        f"Working(第1层)      : {len(snap['working']['frames'])} 帧 / 预算 {snap['working']['budget']}",
        f"引擎统计            : {snap['engine_stats']}",
    ])

    # ---------------------------------------------------------------
    section(f"第 2 步 · 热注水 —— 新请求「{QUERY}」进来, 组装工作记忆")
    h = rt.system.hydrate(USER, QUERY, SYSTEM)

    show(["◆ Profile(静态点查, 零向量, 100%命中):"])
    show([f"   {line}" for line in h["profile"]])
    show(["◆ Episodic(语义向量 Top-K 召回, few-shot):"])
    show([f"   [{r['subject']}] {r['object_value']}  (score={r['score']:.2f})"
          for r in h["recalled"]])
    show(["◆ Semantic(subject 点查规则):"])
    show([f"   [{r['subject']}] {r['object_value']}" for r in h["semantic_hits"]])

    print("\n   ── 模型实际看到的 Context(Working Memory 拼装结果) ──")
    for line in h["context"].splitlines():
        print(f"     | {line}")
    print(f"     └── 共 {len(h['context'])} 字符, 工作记忆 {h['working']['used_units']} tok / {h['working']['budget']}")

    # ---------------------------------------------------------------
    section("第 3 步 · Short-Term 动态折叠：超过窗口, 早期轮次被压成摘要")
    # 换一个小窗口的短期记忆，快一点看到折叠效果
    from memory_engine.tiers.shortterm import ShortTermMemory

    st = ShortTermMemory(window=6, keep_recent=4)
    for i in range(1, 11):
        st.append("user", f"第{i}轮用户追问：NR 切换失败怎么继续定位？")
        st.append("assistant", f"第{i}轮助手回复：建议补查测量报告与目标小区负载。")
    show([f"  窗口内的近期轮次原样保留 {len(st.turns)} 轮(其余已压缩成滚动摘要)"])
    show([f"  折叠摘要(模拟 LLM 折叠, 可替换成真摘要器)累计 {len(st.summary)} 字符:"])
    show([f"    {st.summary[:120]}…"])

    section("第 3.5 步 · Checkpointer：会话断点快照 → 恢复")
    st.note("current_step", "已定位目标小区")
    ckpt = st.checkpoint()
    restored = ShortTermMemory(window=6, keep_recent=4)
    restored.restore(ckpt)
    show([f"快照含 {len(ckpt['turns'])} 轮 + scratchpad={ckpt['scratchpad']}"])
    show([f"恢复后 turns={len(restored.turns)}, scratchpad={restored.scratchpad}"])

    # ---------------------------------------------------------------
    section("第 4 步 · Working 预算：塞入超大工具返回 → 最旧帧被严格入模过滤裁掉")
    from memory_engine.engine import MemoryEngine

    rt2 = build_runtime(AgentMemoryConfig(seed_demo=False))
    wm = rt2.working
    wm.budget = 40  # 故意把预算调小, 演示淘汰
    wm.add("profile", "语言: 中文")
    wm.add("episodic", "回忆: NR 切换失败 | 先看测量报告")
    for f in wm.frames:
        print(f"     保留: [{f.kind}] {f.content[:22]}")
    print(f"     当前 used={wm.used_units()} tok")
    print(f"     >>> 工具返回 200 行日志 试图塞进工作记忆(严格入模过滤)……")
    huge = "retry=200ms reason=timeout " * 40
    wm.add("tool", huge)
    print(f"     塞入后 used={wm.used_units()} tok, 被裁掉 {len(wm.trimmed)} 帧:")
    show([f"     ✂ [{f.kind}] {f.content[:20]}…" for f in wm.trimmed])
    show([f"     仍保留 {len(wm.frames)} 帧: {[f.kind for f in wm.frames]}"])

    banner("结论：确定性走点查(Profile), 经验走向量(Episodic), 会话靠折叠+快照(Short-Term), "
           "窗口有预算(Working)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
