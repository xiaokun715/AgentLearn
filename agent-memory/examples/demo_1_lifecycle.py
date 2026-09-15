"""demo_1：一条记忆的完整生命周期 —— 记忆引擎 8 原语走查。

运行：``python examples/demo_1_lifecycle.py``

覆盖说明书 §全操作协同运转图解：
Insert → Retrieve → Touch(强化/证伪) → Update/Supersede(SCD2) → Consolidate(升阶 SOP)
→ Decay(衰减沉睡) → Wake(唤醒复活)；Delete 收尾(软删/硬删)。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")  # 让脚本能直接 import memory_engine(不依赖已安装)

from memory_engine.config import AgentMemoryConfig
from memory_engine.engine import MemoryEngine
from memory_engine.factory import build_runtime

from _lib import banner, enable_utf8, section, show

USER = "alice"
Q = "排查 5G 基站闪断怎么处理"  # 演示检索用 query


def main() -> int:
    enable_utf8()
    # 干净空库演示(不预置种子),更清楚看到每一步的因果
    rt = build_runtime(AgentMemoryConfig(seed_demo=False))
    eng: MemoryEngine = rt.engine

    banner("DEMO 1 · 一条记忆的一生：引擎 8 原语走查")

    # 1. Insert ------------------------------------------------------------------
    section("① Insert：写入一条新事实")
    ep = eng.remember(
        USER, "5G 基站闪断", "排障经验",
        "先查小区级退服/闪断计数,再看切换失败率与上行干扰,最后核查邻区漏配",
        memory_type="EPISODIC",
    )
    show([f"已写入 {ep.memory_id} v{ep.version} conf={ep.confidence}",
          f"    {USER} {ep.subject} -> {ep.object_value}"])

    # 2. Retrieve ----------------------------------------------------------------
    section("② Retrieve：复合混合检索(sim 0.6 + 置信度 0.2 + 艾宾浩斯 0.2)")
    hits = eng.retrieve(USER, eng.embedder.embed(Q), top_k=3, sim_threshold=0.30)
    show([f"命中 {h.memory_id}  rank={s:.3f}  conf={h.confidence}" for h, s in hits])

    # 3. Touch：强化 --------------------------------------------------------------
    section("③ Touch / Reinforce：被召回且辅助成功 → 突触强化")
    eng.touch_reinforce(ep.memory_id, task_succeeded=True)
    r = eng.get(ep.memory_id)
    show([f"{ep.memory_id}  conf {r.confidence - 0.05:.2f} -> {r.confidence:.2f}, access={r.access_count}"])

    # 4. Update / Supersede (SCD2) ----------------------------------------------
    section("④ Update / Supersede：发现新方法 → 旧版本软失效, version+1 派生新记录")
    old_id = ep.memory_id
    new = eng.update_supersede(old_id, "新增：先抓 S1 信令看切换准备消息再决定是否补邻区")
    show([f"旧 {old_id}  v{ep.version} -> is_active={ep.is_active}, valid_to 已打点"])
    show([f"新 {new.memory_id} v{new.version} conf={new.confidence}, is_active={new.is_active}"])
    show(["版本链:"] + [f"    v{r.version} {'active' if r.is_active else 'archived'} {r.object_value[:26]}…"
                        for r in eng.history(USER, "5G 基站闪断")])

    # 5. Touch：证伪降权 ----------------------------------------------------------
    section("⑤ Touch / Reinforce(失败分支)：被证伪 → 大幅降权, conf<0.35 自动失活")
    victim = eng.remember(USER, "临时经验", "经验", "某个未经验证的捷径")
    for i in range(1, 5):
        eng.touch_reinforce(victim.memory_id, task_succeeded=False)
        v = eng.get(victim.memory_id)
        show([f"第{i}次证伪后 conf={v.confidence:.2f} active={v.is_active}"])
        if not v.is_active:
            break

    # 6. Consolidate --------------------------------------------------------------
    section("⑥ Consolidate：多条碎片合并蒸馏 → 升阶为 SEMANTIC SOP")
    frags = []
    for txt in ["场景A 先查退服计数", "场景B 再核查切换失败率", "场景C 最后补邻区"]:
        frags.append(eng.remember(USER, "5G 基站闪断", "经验", txt, memory_type="EPISODIC"))
    sop = eng.consolidate([f.memory_id for f in frags],
                          "5G 基站闪断：退服计数→切换失败率→补邻区 三步走标准化")
    show([f"SOP {sop.memory_id} type={sop.memory_type} conf={sop.confidence:.2f} predicate={sop.predicate}"])
    show([f"    {sop.object_value}"])
    show(["碎片状态:"] + [f"    {f.memory_id} active={f.is_active}" for f in frags])

    # 7. Decay ---------------------------------------------------------------------
    section("⑦ Decay / Forget：让时间快进 90 天 → 低活力记忆被代谢沉睡")
    # 一条“长期无人访问”的旧经验(半衰期 30 天, 90 天无人问津 → 活力跌破红线)
    lonely = eng.remember(USER, "5G 基站闪断", "备查经验", "某老站点闪断后曾直接换板解决",
                          memory_type="EPISODIC", confidence=0.7)
    eng.simulate_elapsed(days=90)
    slept = eng.run_decay_cycle(half_life_days=30.0, retention_floor=0.2)
    show([f"沉睡 {r.memory_id} ({r.subject}, {r.predicate})  active={r.is_active}"
          for r in slept] or ["本轮没有记忆失活"])

    # 8. Wake ----------------------------------------------------------------------
    section("⑧ Wake / Re-activate：深潜检索命中沉睡冷记忆 → 唤醒复活")
    if slept:
        cold = next(r for r in slept if r.memory_id == lonely.memory_id)
        deep = eng.deep_retrieve(USER, eng.embedder.embed(Q), top_k=5)
        show([f"沉睡记忆仍可被 deep_retrieve 命中: {[d.memory_id for d, _ in deep]}"])
        woken = eng.wake_reactivate(cold.memory_id)
        if woken:
            show([f"{cold.memory_id} 唤醒 -> active={woken.is_active} conf={woken.confidence} "
                  f"(苏醒给中等置信度, 等待再次验证)"])

    # 9. Delete ----------------------------------------------------------------------
    section("⑨ Delete：软删(合规隐藏) / 硬删(物理抹除)")
    target = eng.remember(USER, "待删除记录", "事实", "演示删除")
    eng.delete(target.memory_id, hard_delete=False)
    show([f"软删后 {target.memory_id} 仍在 storage 但 is_active=False ({eng.get(target.memory_id).is_active})"])
    eng.delete(target.memory_id, hard_delete=True)
    show([f"硬删后 {target.memory_id} 已从 storage 移除 (get->{eng.get(target.memory_id)})"])

    banner("最终记忆库统计")
    show([f"{k}: {v}" for k, v in eng.stats().items()])
    return 0


if __name__ == "__main__":
    sys.exit(main())
