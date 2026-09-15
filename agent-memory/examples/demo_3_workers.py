"""demo_3：离线 Worker —— 任务终态的蒸馏沉淀 + 遗忘代谢(时间快进)。

运行：``python examples/demo_3_workers.py``

覆盖说明书 §反思与蒸馏 / §遗忘与衰减 / §经验蒸馏：
* commit_session(): 成功任务 → 沉淀「可行流程」EPISODIC; 失败任务 → 沉淀「避坑教训」;
                    另产出「反思规律」SEMANTIC
* consolidate_sweep(): 同一 subject 碎片≥3 → 合并升阶 SOP
* DecayWorker.run_once(): 快进 90 天后代谢, 低活力 EPISODIC 沉睡
* deep_retrieve + wake: 把刚沉睡的冷经验唤醒复用
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from memory_engine.config import AgentMemoryConfig
from memory_engine.factory import build_runtime
from memory_engine.worker.decay import DecayWorker
from memory_engine.worker.distill import Step

from _lib import banner, enable_utf8, section, show

USER = "alice"
GOAL = "排查 5G 基站闪断"


def main() -> int:
    enable_utf8()
    rt = build_runtime(AgentMemoryConfig(seed_demo=False))
    sys0 = rt.engine.stats()["total"]

    banner("DEMO 3 · 离线 Worker：蒸馏沉淀 + 遗忘代谢")

    # ---------------------------------------------------------------
    section("① 任务 A 成功收官 → 蒸馏出「可行流程」EPISODIC + 「反思规律」SEMANTIC")
    saved_ok = rt.system.commit_session(
        USER, GOAL,
        steps=[
            Step("查小区退服/闪断计数", ok=True),
            Step("看切换失败率", ok=True),
            Step("核查邻区漏配", ok=True),
            Step("重启小区观察", ok=True),
        ],
        outcome=True,
    )
    for rec in saved_ok:
        show([f"   [{rec.memory_type}] {rec.predicate} conf={rec.confidence} | {rec.object_value[:44]}"])

    # ---------------------------------------------------------------
    section("② 任务 B 失败收场 → 蒸馏出「避坑教训」EPISODIC(下次避开该动作)")
    saved_fail = rt.system.commit_session(
        USER, GOAL,
        steps=[
            Step("查小区退服/闪断计数", ok=True),
            Step("直接重启小区", ok=False, detail="未定位根因, 指标无改善"),
        ],
        outcome=False,
    )
    for rec in saved_fail:
        show([f"   [{rec.memory_type}] {rec.predicate} conf={rec.confidence} | {rec.object_value[:60]}"])

    # ---------------------------------------------------------------
    section("③ 同一主题再沉淀三条碎片 → consolidate_sweep 合并升阶 SOP")
    for i, frag in enumerate(["碎片1 退服计数高", "碎片2 切换失败率飙升", "碎片3 邻区漏配"]):
        saved = rt.system.commit_session(
            USER, GOAL, [Step(frag, ok=True)], outcome=True, also_reflect=False
        )
        show([f"   沉淀 {saved[0].memory_id} v{saved[0].version} | {frag}"])
    sop = rt.system.consolidate_sweep(USER, GOAL, min_fragments=3)
    if sop:
        show([f"   SOP 升阶 -> {sop.memory_id} type={sop.memory_type} predicate={sop.predicate}"])
        show([f"       {sop.object_value}"])
    else:
        show(["   (碎片未达阈值)"])

    # ---------------------------------------------------------------
    section("④ DecayWorker：让世界快进 90 天 → 低活力经验被代谢沉睡")
    # 补一条“长期无人访问”的备查经验, 让它成为下一幕要被唤醒的沉睡对象
    lonely = rt.episodic.save_fact(
        USER, GOAL, "备查经验", "同一小区三天内两次闪断,建议直接提交换板处理流程",
    )
    rt.engine.simulate_elapsed(days=90)
    worker = DecayWorker(rt.engine, rt.config)
    slept = worker.run_once()
    show([f"   90 天未访问的 {lonely.memory_id} 也在本轮沉睡名单里: "
          f"{lonely.memory_id in [r.memory_id for r in slept]}"])
    show([f"   本轮共代谢沉睡 {len(slept)} 条:"])
    for r in slept:
        show([f"   o {r.memory_id} ({r.subject}, {r.predicate}) is_active={r.is_active}"])

    # ---------------------------------------------------------------
    section("⑤ 深潜检索 + Wake：另一个站点同样报障 → 唤醒复用刚沉睡的备查经验")
    sleeping_ids = [r.memory_id for r, _ in rt.engine.deep_retrieve(
        USER, rt.engine.embedder.embed("另一个 5G 基站闪断也报障了"), top_k=20) if not r.is_active]
    show([f"   深潜检索能把沉睡记忆也捞出来(共 {len(sleeping_ids)} 条, 含 {lonely.memory_id}: "
          f"{lonely.memory_id in sleeping_ids})"])
    woken = rt.engine.wake_reactivate(lonely.memory_id)
    show([f"   wake {lonely.memory_id} -> conf={woken.confidence} active={woken.is_active} "
          f"(苏醒给中等置信度, 重新进入可召回池)"])
    show([f"   该经验: {lonely.subject} | {lonely.object_value[:34]}…"])

    banner("沉淀-合并-代谢 三轮之后的记忆库")
    show([f"   引擎内存总条数: {rt.engine.stats()['total']} (起点 {sys0})"])
    show([f"   by_type: {rt.engine.stats()['by_type']}"])
    show([f"   by_state: {rt.engine.stats()['by_state']}"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
