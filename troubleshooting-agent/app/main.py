"""排障智能体 —— 命令行演示入口。

覆盖《第十五章：排障 Agent 设计》里最值得看的那几条主线：

    python -m app.main --list      # 看有哪些场景
    python -m app.main all         # 全跑
    python -m app.main diagnose    # 只跑指定场景
    python -m app.main tests       # 带断言的回归测试

设计说明：与本书其它 Demo 一样，**没有 HTTP API 层**。排障系统的接口形态
（REST / gRPC / MQ）会因为接入方不同而不同，但「证据分层、假设更新、
信息增益选检查、受控恢复」这些语义是稳定的 —— 入口做成 CLI，
考察的重点才落得准。
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable

from pydantic import ValidationError

from .classification.classifier import FailureClassifier, explain
from .domain.enums import (
    ExperienceStatus,
    FailureCategory,
    Observation,
    RiskTier,
    TerminalState,
)
from .domain.models import Budget, DiagnosticPlan, PlanNode
from .domain.policy import APPROVAL_MATRIX, CONCLUSION, template_for
from .factory import PlatformConfig, build_platform, check_config
from .hypothesis.manager import HypothesisManager
from .sim import BASE_TIME

WIDTH = 78


# ======================================================================
# 终端输出小工具
# ======================================================================
def banner(text: str) -> None:
    print()
    print("=" * WIDTH)
    print(f"  {text}")
    print("=" * WIDTH)


def section(index: int, title: str, ref: str = "") -> None:
    suffix = f"  [{ref}]" if ref else ""
    print(f"\n── {index}. {title}{suffix}")
    print("─" * WIDTH)


def step(text: str) -> None:
    print(f"   → {text}")


def info(key: str, value: object) -> None:
    print(f"     {key:<24} {value}")


def ok(text: str) -> None:
    print(f"   ✓ {text}")


def warn(text: str) -> None:
    print(f"   ! {text}")


def brief(text: str, limit: int = 120) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def show_hypotheses(manager) -> None:
    """把假设集合渲染成一张表。"""
    for item in manager.summary():
        mark = {"SUPPORTED": "✓", "REFUTED": "✗", "UNDECIDED": "?", "PENDING": "·"}[item["status"]]
        print(
            f"     {mark} {item['key']:<11} {item['status']:<10} "
            f"先验={item['prior']:.2f} 后验={item['posterior']:.2f} "
            f"支持={item['supporting']:>2} 反驳={item['contradicting']:>2}"
        )


def show_notes(conclusion, limit: int = 100) -> None:
    for note in conclusion.notes:
        if note.startswith("第 ") or "结论门" in note:
            print(f"     · {brief(note, limit)}")
        else:
            print(f"     · {brief(note, limit)}")


# ======================================================================
# 场景 1：故障分类（规则优先 + LLM 补充）
# ======================================================================
def scenario_classify() -> None:
    banner("场景一：故障分类 —— 规则优先，规则命不中就 UNKNOWN（§4.2）")

    section(1, "规则命中：轨迹里出现明确的环境错误码", "§4.1")
    platform = build_platform(world="env")
    agent = platform.build_agent()
    trace = agent.analyzer.analyze(platform.world.context)
    classification = FailureClassifier(llm=platform.llm).classify(platform.world.context, trace)
    info("分类结果", explain(classification))
    info("判定依据", brief(classification.rationale, 110))
    info("建议检查", classification.next_checks)
    step("规则读的是**确定性输入**：错误码、返回码、轨迹结构化字段，不是「模型觉得像」")

    section(2, "规则命不中：交给 LLM 提候选类别", "§4.2")
    platform2 = build_platform(world="config")
    agent2 = platform2.build_agent()
    trace2 = agent2.analyzer.analyze(platform2.world.context)
    classification2 = FailureClassifier(llm=platform2.llm).classify(platform2.world.context, trace2)
    info("错误码", platform2.world.context.error_codes)
    info("分类结果", explain(classification2))
    info("候选原因", classification2.candidate_causes)
    warn("注意置信度 0.55 是**估计值**，不是校准过的概率 —— 它只用于排序（§4.2）")

    section(3, "证据太少：允许返回 UNKNOWN，而不是硬猜", "§4.2")
    platform3 = build_platform(world="sparse")
    agent3 = platform3.build_agent()
    trace3 = agent3.analyzer.analyze(platform3.world.context)
    classification3 = FailureClassifier(llm=platform3.llm).classify(platform3.world.context, trace3)
    info("分类结果", explain(classification3))
    info("判定依据", brief(classification3.rationale, 110))
    ok("UNKNOWN 是一等公民：规则没命中不等于规则失败，它是「待收集更多证据」")


# ======================================================================
# 场景 2：执行轨迹分析（失败节点 ≠ 根因节点）
# ======================================================================
def scenario_trace() -> None:
    banner("场景二：执行轨迹分析 —— 失败节点不一定是根因节点（§12 第一问）")

    platform = build_platform(world="env")
    agent = platform.build_agent()
    trace = agent.analyzer.analyze(platform.world.context)
    index = {event.event_id: event for event in platform.world.context.execution_trace}

    section(1, "轨迹本身（注意每个节点的 status 与 metadata）", "§3.2")
    for event in platform.world.context.execution_trace:
        parent = event.parent_event_id or "-"
        print(
            f"     {event.event_id}  {event.skill or event.event_type:<24} "
            f"status={event.status:<7} parent={parent}"
        )
        if event.metadata:
            info("metadata", brief(event.metadata, 90))
        if event.error_code:
            info("error_code", event.error_code)

    section(2, "报错的节点（现象节点）", "§12")
    phenomenon = index.get(trace.phenomenon_node or "")
    info("现象节点", f"{trace.phenomenon_node} ({phenomenon.skill if phenomenon else '-'})")
    info("它的错误", f"{phenomenon.error_code if phenomenon else '-'}")
    step("只看这一行，结论会是「采集工具坏了」—— 但那是现象，不是原因")

    section(3, "上游的「假成功」节点（疑似根因）", "§12")
    for anomaly in trace.anomalies:
        info(anomaly["event_id"], f"{anomaly.get('skill')}  kind={anomaly['kind']}")
        for signal in anomaly["signals"]:
            print(f"        ↳ {signal}")

    section(4, "结论：这条轨迹里谁才是嫌疑人", "§12")
    info("疑似根因节点", trace.suspect_nodes)
    info("现象节点即根因？", trace.phenomenon_is_root)
    for note in trace.notes:
        print(f"     · {note}")
    ok(
        "判据全部来自结构化字段（verified / output_ref / result_complete / "
        "下游的 precondition_violation），不靠模型「感觉不对」"
    )

    section(5, "轨迹只调先验，不计成证据", "§5.1")
    c = platform.build_agent().troubleshoot(platform.world.context)
    for note in c.notes:
        if "轨迹线索" in note:
            print(f"     · {note}")
    info("结论的原始证据锚点", c.metrics["raw_evidence_anchors"])
    step("「看起来很可疑」把先验抬高，但结论门数的仍是**执行过的检查**产出的证据")


# ======================================================================
# 场景 3：证据驱动的动态诊断
# ======================================================================
def scenario_diagnose() -> None:
    banner("场景三：证据驱动的动态诊断 —— 下一步检查什么，由证据决定（§5.3/§6.2）")

    # ---------------------------------------------------------------- 3.1
    section(1, "SRS 测量失败：初始假设集合", "§5.1")
    platform = build_platform(world="env")
    agent = platform.build_agent()
    trace = agent.analyzer.analyze(platform.world.context)
    classification = FailureClassifier(llm=platform.llm).classify(platform.world.context, trace)
    manager = HypothesisManager.from_classification(classification)
    info("分类", explain(classification))
    show_hypotheses(manager)
    info("集合熵 H(ℋ)", f"{manager.entropy():.3f} bit（越大越不确定）")

    section(2, "第一轮：按信息增益给所有候选检查打分", "§5.3")
    for scored in platform.planner.rank(manager):
        print(f"     {scored.explain()}")
    best = platform.planner.best(manager)
    step(f"选中 {best.spec.name}：" + "；".join(platform.planner.rank(manager)[0].spec.produces))
    step("评分 = IG / (Cost + λ·Risk + ε) —— 不是「哪个工具看起来最相关」（§5.3）")

    section(3, "执行检查 → 观测 → 贝叶斯更新", "§5.3")
    spec = best.spec
    observation = platform.registry.resolve(spec.name)(platform.tool_ctx, PlanNode(id="N1", skill=spec.name))
    report = manager.update(spec, observation.outcome, ["EV_demo"])
    info("观测结果", observation.outcome.value)
    info("摘要", brief(observation.summary, 100))
    info("预期信息增益", f"{manager.expected_information_gain(spec):.3f}")
    info("实际信息增益", f"{report.information_gain:.3f}")
    step(
        "预期是对所有可能观测的加权平均；这次命中了小概率但高区分力的分支，"
        "所以实际值反超预期 —— 这个差值本身说明「这次检查很值」"
    )
    for key, before, after in report.moved:
        print(f"     {key:<11} {before:.3f} → {after:.3f}")
    show_hypotheses(manager)

    section(4, "结论门：三条硬门槛，全部是确定性判据", "§10.3 第 1 条")
    ready, reason = manager.conclusion_ready(lambda eid: "demo")
    info("是否可下结论", ready)
    info("判据", brief(reason, 110))
    if ready:
        ok(f"一次决定性检查（单次把后验推高 ≥ {CONCLUSION.decisive_move:.2f}）足以定论")

    section(5, "完整闭环：动态模式 vs 预定义模板", "§6.2")
    p_dyn = build_platform(world="env")
    c_dyn = p_dyn.troubleshoot(use_templates=False)
    p_tpl = build_platform(world="env")
    c_tpl = p_tpl.troubleshoot(use_templates=True)
    for label, conclusion in (("动态（模式 C）", c_dyn), ("模板（模式 A）", c_tpl)):
        info(
            label,
            f"{conclusion.terminal_state.value}  根因={conclusion.root_cause_key}  "
            f"诊断动作数={conclusion.metrics['distinct_diagnostic_actions']}  "
            f"工具调用={conclusion.metrics['tool_calls']}  模式={conclusion.metrics['plan_modes']}",
        )
    step("模板把 4 个检查全跑完；动态模式只跑那一个能定性的 —— 这就是 §5.2 的价值")

    # ---------------------------------------------------------------- 3.2
    section(6, "反例：当初始分类是错的（LLM 猜了 CONFIG）", "§5.3")
    p_case = build_platform(world="case")
    c_case = p_case.troubleshoot(use_templates=False)
    info("初始分类", explain(c_case.classification))
    info("最终根因", f"{c_case.root_cause_key}（后验 {c_case.confidence:.2f}）")
    info("诊断路径", c_case.diagnostic_path)
    info("工具调用数", c_case.metrics["tool_calls"])
    print()
    print("     每轮的假设变化：")
    for note in c_case.notes:
        if "→" in note and ("观测=" in note):
            print(f"       · {note}")
    ok("分类只是起点：检查结果把 CONFIG 压到 0.10，把 CASE 推到 0.79（§5.3）")


# ======================================================================
# 场景 4：动态 Workflow 与校验器
# ======================================================================
def scenario_workflow() -> None:
    banner("场景四：动态 Workflow 与 Workflow Validator（§6.2/§6.3）")

    section(1, "模式 A：预定义模板", "§6.2")
    platform = build_platform(world="env")
    c = platform.troubleshoot(use_templates=True, auto_approve=False)
    info("使用模式", c.metrics["plan_modes"])
    template = template_for(FailureCategory.ENVIRONMENT)
    info("模板说明", brief(template.description if template else "-", 140))
    info("模板节点", [node[1] for node in template.nodes] if template else [])

    section(2, "模式 B：模板裁剪 —— 复用已有结果，不重复执行", "§6.2")
    platform2 = build_platform(world="env")
    platform2.world.context.cached_checks = {
        "environment_state": {
            "observation": Observation.ANOMALY.value,
            "software_version": "V100R020C10",
            "collected_at": BASE_TIME.isoformat(),
        },
        "test_config": {
            "observation": Observation.NORMAL.value,
            "software_version": "V100R020C10",
            "collected_at": BASE_TIME.isoformat(),
        },
    }
    c2 = platform2.build_agent().troubleshoot(platform2.world.context, use_templates=True)
    info("使用模式", c2.metrics["plan_modes"])
    info("工具调用数", f"{c2.metrics['tool_calls']}（未裁剪时需要 4 次）")
    for note in c2.notes:
        if "复用" in note:
            print(f"     · {note}")
    step("复用前必须校验有效期/版本/上下文 —— 不能因为「历史上成功过」就直接用")
    platform2.world.context.cached_checks["environment_state"]["software_version"] = "V100R010C00"
    plan, mode = platform2.planner.build_from_template(
        platform2.world.context, template_for(FailureCategory.ENVIRONMENT)
    )
    info("把版本改成 V100R010C00 后", f"模式={mode.value}")
    for node in plan.nodes:
        info(f"节点 {node.id} {node.skill}", f"reused_from={node.reused_from}")
    warn(
        "只有那个版本对不上的缓存作废了 —— 裁剪是**逐个节点**判定的，"
        "不是整份计划一刀切"
    )

    section(3, "模式 C：动态生成（含并行分支的资源冲突判断）", "§6.2")
    platform3 = build_platform(PlatformConfig(branching=True), world="env")
    agent3 = platform3.build_agent()
    trace3 = agent3.analyzer.analyze(platform3.world.context)
    classification3 = FailureClassifier(llm=platform3.llm).classify(platform3.world.context, trace3)
    manager3 = HypothesisManager.from_classification(classification3)
    plan3 = platform3.planner.build_dynamic(platform3.world.context, manager3)
    info("计划说明", brief(plan3.rationale, 130))
    info("节点", [f"{n.id}:{n.skill}" for n in plan3.nodes])
    info("可并行分组", platform3.executor.parallel_groups(plan3))
    info("校验结论", platform3.validator.validate(plan3, budget=platform3.config.budget).ok)

    section(4, "校验器：七类非法计划，一个都过不去", "§6.3")
    cases = [
        ("valid", "合法计划"),
        ("cyclic", "模型写出了环"),
        ("unauthorized", "模型把恢复动作塞进诊断计划"),
        ("unknown_skill", "模型发明了不存在的 skill"),
        ("schema_mismatch", "输入契约与依赖图不一致"),
        ("resource_conflict", "两个节点争抢同一台环境"),
        ("budget", "节点数超预算"),
    ]
    for style, label in cases:
        config = PlatformConfig.for_demo()
        config.plan_style = style
        config.use_llm_planner = True
        p = build_platform(config, world="env")
        a = p.build_agent()
        t = a.analyzer.analyze(p.world.context)
        cl = FailureClassifier(llm=p.llm).classify(p.world.context, t)
        m = HypothesisManager.from_classification(cl)
        proposal = p.planner.build_from_llm(p.world.context, m)
        report = p.validator.validate(proposal, p.world.context, budget=config.budget)
        mark = "✓ 通过" if report.ok else "✗ 拦下"
        detail = "；".join(f"[{i.code}]{i.node_id or ''}" for i in report.blocking)
        info(f"{label}", f"{mark}  {detail}")
    ok("模型只输出结构化节点（没有代码），但依然要过确定性校验（§6.3 第 1 条禁令）")


# ======================================================================
# 场景 5：受控恢复
# ======================================================================
def scenario_recover() -> None:
    banner("场景五：受控恢复 —— 分级、审批、验证门（§7.1/§7.2）")

    section(1, "风险分级表（R0~R4 与审批策略）", "§7.1")
    for tier in RiskTier:
        note = {
            RiskTier.R0: "查询日志、读取配置",
            RiskTier.R1: "无副作用的状态刷新、只读校验",
            RiskTier.R2: "重新申请环境、重跑可重复测试",
            RiskTier.R3: "重启设备、修改配置",
            RiskTier.R4: "改测试预期、覆盖正式配置、不可逆操作",
        }[tier]
        info(f"{tier.value} -> {APPROVAL_MATRIX[tier]}", note)

    section(2, "R2 自动恢复：先试最小的动作，无效再升级", "§7.1/§7.2")
    platform = build_platform(world="env")
    c = platform.troubleshoot(use_templates=False)
    info("终止状态", c.terminal_state.value)
    for attempt in c.recovery:
        info(
            f"第 {attempt.attempt} 次恢复",
            f"{attempt.action}（{attempt.tier.value}） 执行={attempt.executed} "
            f"环境恢复={attempt.verified} 复测={attempt.rerun_passed}",
        )
    info("恢复前快照", c.recovery[0].before_snapshot)
    info("恢复后快照", c.recovery[-1].after_snapshot)
    warn("复测通过只证明「当前环境下问题不再复现」，不等于根因已被独立确认（§7.2）")

    section(3, "R3 需要审批：没有批准就停在 NEEDS_HUMAN", "§7.2")
    platform2 = build_platform(world="device-hung")
    c2 = platform2.troubleshoot(use_templates=False)
    info("终止状态", c2.terminal_state.value)
    info("根因（已经定位了）", c2.root_cause_key)
    for attempt in c2.recovery:
        info(f"尝试 {attempt.action}", f"执行={attempt.executed} 环境恢复={attempt.verified}")
    for note in c2.notes:
        if "需要人工审批" in note:
            print(f"     · {brief(note, 130)}")
    ok("R3 走不通就停住，并说明「批准什么」—— 不擅自重启设备")

    section(4, "批准之后：同一个动作继续往下走", "§7.2")
    platform3 = build_platform(world="device-hung")
    c3 = platform3.troubleshoot(use_templates=False, auto_approve=True)
    info("终止状态", c3.terminal_state.value)
    for attempt in c3.recovery:
        info(f"尝试 {attempt.action}", f"环境恢复={attempt.verified} 复测={attempt.rerun_passed}")
    info("审批记录", sorted(platform3.recovery.approved_actions))

    section(5, "R4 连审批通道都没有", "§7.1")
    platform4 = build_platform(world="config")
    plan = DiagnosticPlan(
        nodes=[PlanNode(id="X1", skill="override_test_expectation", depends_on=[])],
        rationale="想「修正」预期结果让测试通过",
    )
    report = platform4.validator.validate(plan, budget=platform4.config.budget)
    for issue in report.issues:
        info(issue.code, brief(issue.message, 110))
    try:
        platform4.recovery.grant("override_test_expectation")
    except PermissionError as exc:
        warn(f"申请审批被拒：{exc}")
    step("改测试预期会让「通过率」变好看，但不是排障 —— 这是 §9 明确点名的反模式")


# ======================================================================
# 场景 6：无法定位时，如实说 UNRESOLVED
# ======================================================================
def scenario_unresolved() -> None:
    banner("场景六：证据不足时输出 UNRESOLVED，不强行选一个根因（§9/§10.3 第 7 条）")

    section(1, "证据稀疏的故障：日志已轮转、探针超时、配置读不到", "§9")
    config = PlatformConfig.for_demo()
    config.budget = Budget(max_steps=3, max_tool_calls=6, max_seconds=600, max_recovery_attempts=3)
    platform = build_platform(config, world="sparse")
    c = platform.troubleshoot(use_templates=False)
    info("终止状态", c.terminal_state.value)
    info("根因", c.root_cause)
    info("未定位原因", brief(c.unresolved_reason or "", 130))
    info("跑过的检查", c.diagnostic_path)
    info("工具调用数", c.metrics["tool_calls"])

    section(2, "假设的最终状态：UNDECIDED 而不是 REFUTED", "§5.1")
    for item in c.hypotheses:
        info(f"{item['key']} {item['status']}", f"后验={item['posterior']:.2f}")
    ok(
        "「没查」被标成 UNDECIDED、「查过且被推翻」才是 REFUTED —— "
        "把两者混在一起，是排障系统最危险的自我欺骗（§5.1）"
    )

    section(3, "这类结论不会被写成经验", "§8.2")
    info("经验库决策", c.metrics["experience"]["decision"])
    info("理由", c.metrics["experience"]["reasons"])
    step("未定位的排障只保留证据，不产生经验 —— 否则库里的「经验」会越攒越假")


# ======================================================================
# 场景 7：故障经验库
# ======================================================================
def scenario_experience() -> None:
    banner("场景七：故障经验库 —— 质量门控、检索复用、失败经验（§8）")

    section(1, "第一次排障 → 提取候选经验 → 质量门控", "§8.2")
    platform = build_platform(world="env")
    c = platform.troubleshoot(use_templates=False)
    extraction = c.metrics["experience"]
    info("终止状态", c.terminal_state.value)
    info("入库决策", extraction["decision"])
    info("case_id", extraction["case_id"])
    for reason in extraction["reasons"]:
        print(f"     · {reason}")

    section(2, "同一次排障里，无效的恢复动作也会留下记录", "§8.2")
    for case in platform.experience.by_status(ExperienceStatus.FAILED):
        info(case.case_id, f"FAILED  无效动作={case.recovery_actions}")
    step("说明书原话：尤其需要保留失败经验，否则系统可能反复尝试已经证明无效的方法")

    section(3, "第二次排障：检索历史案例 → 校验适用条件 → 加权先验", "§8.3")
    platform2 = build_platform(world="env")
    platform2.experience = platform.experience        # 把经验库带过来
    c2 = platform2.build_agent().troubleshoot(platform2.world.context, use_templates=False)
    for note in c2.notes:
        if note.startswith(("经验检索", "历史案例", "跳过")):
            print(f"     · {brief(note, 130)}")
    info("本次工具调用数", c2.metrics["tool_calls"])

    section(4, "不适用的案例不会被套用", "§8.3")
    platform3 = build_platform(world="env")
    platform3.experience = platform.experience
    platform3.world.context.software_version = "V100R030C00"   # 换一个软件版本
    retrievals = platform3.experience.retrieve(platform3.world.context)
    for item in retrievals:
        info(item.case.case_id, f"相似度={item.score:.2f} 适用={item.applicable} ({item.reason})")
    ok("说明书原话：不能因为两个任务的错误描述相似，就直接套用相同的恢复操作")

    section(5, "经验库现状", "§8.1")
    info("统计", platform.experience.stats())
    for case in platform.experience.cases():
        root = case.verified_root_cause.key if case.verified_root_cause else "-"
        info(
            case.case_id,
            f"{case.status.value:<10} 根因={root:<10} 复用次数={case.reused_count} "
            f"生效={case.effective}",
        )


# ======================================================================
# 回归测试（带断言）
# ======================================================================
class _Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append((name, passed, detail))
        print(f"   {'✓' if passed else '✗'} {name}")
        if detail:
            print(f"       {detail}")

    def report(self) -> int:
        passed = sum(1 for _, ok_, _ in self.results if ok_)
        total = len(self.results)
        print()
        print("=" * WIDTH)
        print(f"  回归测试结果：{passed}/{total} 通过")
        print("=" * WIDTH)
        for name, ok_, _ in self.results:
            if not ok_:
                print(f"   ✗ FAILED: {name}")
        return 0 if passed == total else 1


def scenario_tests() -> int:
    banner("回归测试 —— 排障系统的核心不变量")
    checks = _Checks()

    # ---------------------------------------------------------------- T1
    print("\n[T1] 轨迹分析：失败节点不是根因节点")
    platform = build_platform(world="env")
    trace = platform.build_agent().analyzer.analyze(platform.world.context)
    checks.check(
        "识别出现象节点",
        trace.phenomenon_node is not None,
        f"现象节点={trace.phenomenon_node}",
    )
    checks.check(
        "回溯出上游的假成功节点",
        trace.suspect_nodes == ["EVENT_002"],
        f"疑似根因={trace.suspect_nodes}",
    )
    checks.check(
        "现象节点被判为「不是根因」",
        not trace.phenomenon_is_root,
        "phenomenon_is_root=False",
    )

    # ---------------------------------------------------------------- T2
    print("\n[T2] 分类：规则优先，命不中走 LLM，证据不足给 UNKNOWN")
    p_env = build_platform(world="env")
    a_env = p_env.build_agent()
    c_env = FailureClassifier(llm=p_env.llm).classify(
        p_env.world.context, a_env.analyzer.analyze(p_env.world.context)
    )
    checks.check(
        "环境错误码走规则分类",
        c_env.decided_by == "rule" and c_env.primary_category is FailureCategory.ENVIRONMENT,
        f"{c_env.primary_category.value} via {c_env.decided_by}",
    )
    p_cfg = build_platform(world="config")
    a_cfg = p_cfg.build_agent()
    c_cfg = FailureClassifier(llm=p_cfg.llm).classify(
        p_cfg.world.context, a_cfg.analyzer.analyze(p_cfg.world.context)
    )
    checks.check(
        "无法用规则定性的故障走 LLM 兜底",
        c_cfg.decided_by == "llm",
        f"category={c_cfg.primary_category.value} via {c_cfg.decided_by}",
    )
    p_sp = build_platform(world="sparse")
    a_sp = p_sp.build_agent()
    c_sp = FailureClassifier(llm=p_sp.llm).classify(
        p_sp.world.context, a_sp.analyzer.analyze(p_sp.world.context)
    )
    checks.check(
        "证据不足时允许 UNKNOWN（不是硬猜）",
        c_sp.primary_category is FailureCategory.UNKNOWN,
        f"category={c_sp.primary_category.value} via {c_sp.decided_by}",
    )

    # ---------------------------------------------------------------- T3
    print("\n[T3] 信息增益：能区分假设的检查得分高，重复确认的得分是 0")
    p = build_platform(world="env")
    a = p.build_agent()
    t = a.analyzer.analyze(p.world.context)
    cl = FailureClassifier(llm=p.llm).classify(p.world.context, t)
    manager = HypothesisManager.from_classification(cl)
    ranked = {item.spec.name: item for item in p.planner.rank(manager)}
    checks.check(
        "针对性检查的 IG 明显高于广谱检查",
        ranked["check_environment_state"].information_gain
        > ranked["analyze_logs"].information_gain,
        f"env={ranked['check_environment_state'].information_gain:.3f} vs "
        f"logs={ranked['analyze_logs'].information_gain:.3f}",
    )
    failure_cases_spec = p.pool.require("retrieve_failure_cases")
    checks.check(
        "历史案例检索的信息增益恒为 0（它不区分根因）",
        abs(manager.expected_information_gain(failure_cases_spec)) < 1e-9,
        f"IG={manager.expected_information_gain(failure_cases_spec):.6f}",
    )
    checks.check(
        "它因此不会出现在候选集里",
        "retrieve_failure_cases" not in ranked,
        f"候选={sorted(ranked)}",
    )

    # ---------------------------------------------------------------- T4
    print("\n[T4] 假设更新：异常抬升、正常排除")
    manager2 = HypothesisManager.from_classification(cl)
    before = manager2.get("H_ENV").posterior
    spec = p.pool.require("check_environment_state")
    manager2.update(spec, Observation.ANOMALY, ["EV_x"])
    after = manager2.get("H_ENV").posterior
    checks.check(
        "环境异常把 H_ENV 抬上去",
        after > before,
        f"{before:.3f} → {after:.3f}",
    )
    manager3 = HypothesisManager.from_classification(cl)
    manager3.update(spec, Observation.NORMAL, ["EV_y"])
    checks.check(
        "环境正常把 H_ENV 压下去",
        manager3.get("H_ENV").posterior < manager3.get("H_ENV").prior,
        f"prior={manager3.get('H_ENV').prior:.3f} → post={manager3.get('H_ENV').posterior:.3f}",
    )
    checks.check(
        "INCONCLUSIVE 不改变任何假设",
        manager3.update(spec, Observation.INCONCLUSIVE, []).moved == [],
        "moved=[]",
    )

    # ---------------------------------------------------------------- T5
    print("\n[T5] 结论门：不确定就不下结论")
    manager4 = HypothesisManager.from_classification(cl)
    ready, reason = manager4.conclusion_ready()
    checks.check(
        "什么都没查时不允许下结论",
        not ready,
        f"{reason}",
    )
    manager4.update(spec, Observation.ANOMALY, ["EV_a"])
    ready2, reason2 = manager4.conclusion_ready()
    checks.check(
        "一次决定性检查后允许下结论",
        ready2,
        f"{reason2}",
    )

    # ---------------------------------------------------------------- T6
    print("\n[T6] Workflow Validator：七类非法计划全部拦下")
    styles = {
        "cyclic": "CYCLE_DETECTED",
        "unauthorized": "UNAUTHORIZED_RECOVERY",
        "unknown_skill": "UNKNOWN_SKILL",
        "schema_mismatch": "SCHEMA_MISMATCH",
        "resource_conflict": "RESOURCE_CONFLICT",
        "budget": "BUDGET_NODE_COUNT",
    }
    for style, expected in styles.items():
        config = PlatformConfig.for_demo()
        config.plan_style = style
        config.use_llm_planner = True
        pp = build_platform(config, world="env")
        aa = pp.build_agent()
        tt = aa.analyzer.analyze(pp.world.context)
        cc = FailureClassifier(llm=pp.llm).classify(pp.world.context, tt)
        mm = HypothesisManager.from_classification(cc)
        plan = pp.planner.build_from_llm(pp.world.context, mm)
        report = pp.validator.validate(plan, pp.world.context, budget=config.budget)
        codes = {issue.code for issue in report.blocking}
        checks.check(
            f"{style} -> {expected}",
            expected in codes,
            f"issues={sorted(codes)}",
        )
    config_ok = PlatformConfig.for_demo()
    pp = build_platform(config_ok, world="env")
    aa = pp.build_agent()
    tt = aa.analyzer.analyze(pp.world.context)
    cc = FailureClassifier(llm=pp.llm).classify(pp.world.context, tt)
    mm = HypothesisManager.from_classification(cc)
    plan = pp.planner.build_from_llm(pp.world.context, mm)
    checks.check(
        "合法计划不被误伤",
        pp.validator.validate(plan, budget=config_ok.budget).ok,
        "ok=True",
    )

    # ---------------------------------------------------------------- T7
    print("\n[T7] 根因定位：三类故障都能被证据捞出来")
    for world, expected in (("env", "H_ENV"), ("config", "H_CONFIG"), ("case", "H_CASE")):
        pp = build_platform(world=world)
        cc = pp.troubleshoot(use_templates=False)
        checks.check(
            f"{world} -> {expected}",
            cc.root_cause_key == expected
            and cc.terminal_state in (TerminalState.RESOLVED, TerminalState.ROOT_CAUSE_IDENTIFIED),
            f"根因={cc.root_cause_key} 状态={cc.terminal_state.value} "
            f"调用={cc.metrics['tool_calls']}",
        )

    # ---------------------------------------------------------------- T8
    print("\n[T8] 动态模式不比重（同世界：模板 4 个检查 vs 动态 1 个）")
    p_dyn = build_platform(world="env")
    c_dyn = p_dyn.troubleshoot(use_templates=False)
    p_tpl = build_platform(world="env")
    c_tpl = p_tpl.troubleshoot(use_templates=True)
    checks.check(
        "动态模式跑的诊断动作少于模板模式",
        c_dyn.metrics["distinct_diagnostic_actions"]
        < c_tpl.metrics["distinct_diagnostic_actions"],
        f"动态={c_dyn.metrics['distinct_diagnostic_actions']} "
        f"模板={c_tpl.metrics['distinct_diagnostic_actions']}",
    )
    checks.check(
        "两条路径给出同一个根因",
        c_dyn.root_cause_key == c_tpl.root_cause_key == "H_ENV",
        f"{c_dyn.root_cause_key} / {c_tpl.root_cause_key}",
    )

    # ---------------------------------------------------------------- T9
    print("\n[T9] 受控恢复：阶梯升级、审批、复测验证")
    p_rec = build_platform(world="env")
    c_rec = p_rec.troubleshoot(use_templates=False)
    checks.check(
        "R1 无效后升级到 R2，最终 RESOLVED",
        c_rec.terminal_state is TerminalState.RESOLVED
        and [r.action for r in c_rec.recovery]
        == ["refresh_environment_state", "reallocate_environment"],
        f"状态={c_rec.terminal_state.value} "
        f"轨迹={[r.action for r in c_rec.recovery]}",
    )
    checks.check(
        "复测结果被单独记录（不把「复测通过」当成「根因确认」）",
        c_rec.recovery[-1].rerun_passed is True
        and c_rec.recovery[-1].verified is True
        and c_rec.recovery[-1].before_snapshot != c_rec.recovery[-1].after_snapshot,
        "before/after 快照不同且 rerun_passed=True",
    )
    p_hung = build_platform(world="device-hung")
    c_hung = p_hung.troubleshoot(use_templates=False)
    checks.check(
        "R3 未获批准 -> NEEDS_HUMAN（不擅自重启设备）",
        c_hung.terminal_state is TerminalState.NEEDS_HUMAN
        and all(not r.executed for r in c_hung.recovery if r.action == "restart_device")
        if any(r.action == "restart_device" for r in c_hung.recovery)
        else c_hung.terminal_state is TerminalState.NEEDS_HUMAN,
        f"状态={c_hung.terminal_state.value} 根因={c_hung.root_cause_key}",
    )
    p_hung2 = build_platform(world="device-hung")
    c_hung2 = p_hung2.troubleshoot(use_templates=False, auto_approve=True)
    checks.check(
        "批准后 R3 执行并 RESOLVED",
        c_hung2.terminal_state is TerminalState.RESOLVED
        and "restart_device" in [r.action for r in c_hung2.recovery],
        f"状态={c_hung2.terminal_state.value}",
    )

    # ---------------------------------------------------------------- T10
    print("\n[T10] R4 默认禁止自动执行")
    p_cfg = build_platform(world="config")
    plan = DiagnosticPlan(nodes=[PlanNode(id="X", skill="override_test_expectation")])
    report = p_cfg.validator.validate(plan, budget=p_cfg.config.budget)
    checks.check(
        "R4 动作被 FORBIDDEN_RECOVERY 拦下",
        any(issue.code == "FORBIDDEN_RECOVERY" for issue in report.blocking),
        f"issues={[i.code for i in report.blocking]}",
    )
    try:
        p_cfg.recovery.grant("override_test_expectation")
        granted = True
    except PermissionError:
        granted = False
    checks.check(
        "R4 连审批通道都不开放",
        not granted,
        "grant() 抛出 PermissionError",
    )

    # ---------------------------------------------------------------- T11
    print("\n[T11] 无法定位时如实输出 UNRESOLVED，且各假设标 UNDECIDED")
    config = PlatformConfig.for_demo()
    config.budget = Budget(max_steps=2, max_tool_calls=6, max_seconds=600, max_recovery_attempts=3)
    p_uns = build_platform(config, world="sparse")
    c_uns = p_uns.troubleshoot(use_templates=False)
    checks.check(
        "终止状态为 UNRESOLVED",
        c_uns.terminal_state is TerminalState.UNRESOLVED,
        f"{c_uns.terminal_state.value}：{brief(c_uns.unresolved_reason or '', 80)}",
    )
    checks.check(
        "根因没有被强行填上",
        c_uns.root_cause is None and c_uns.root_cause_key is None,
        "root_cause=None",
    )
    checks.check(
        "未检验的假设标为 UNDECIDED 而非 REFUTED",
        all(item["status"] in ("UNDECIDED", "PENDING") for item in c_uns.hypotheses),
        f"状态={[i['status'] for i in c_uns.hypotheses]}",
    )
    checks.check(
        "未定位的排障不产生经验",
        c_uns.metrics["experience"]["decision"] == "REJECT_UNRESOLVED",
        c_uns.metrics["experience"]["decision"],
    )

    # ---------------------------------------------------------------- T12
    print("\n[T12] 经验库：质量门控、失败经验、适用性校验")
    p_exp = build_platform(world="env")
    c_exp = p_exp.troubleshoot(use_templates=False)
    checks.check(
        "已验证的排障结果入库为 VERIFIED",
        c_exp.metrics["experience"]["decision"] == "PUBLISH_VERIFIED",
        f"{c_exp.metrics['experience']}",
    )
    failed = p_exp.experience.by_status(ExperienceStatus.FAILED)
    checks.check(
        "无效的恢复动作留下失败经验",
        any("refresh_environment_state" in case.recovery_actions for case in failed),
        f"失败经验={[(c.case_id, c.recovery_actions) for c in failed]}",
    )
    checks.check(
        "失败经验不会被用于复用恢复动作",
        all(
            not item.applicable
            for item in p_exp.experience.retrieve(p_exp.world.context)
            if item.case.status is ExperienceStatus.FAILED
        ),
        "FAILED 案例 applicable=False",
    )
    p_exp2 = build_platform(world="env")
    p_exp2.experience = p_exp.experience
    p_exp2.world.context.software_version = "V100R999C99"
    stale = [
        item for item in p_exp2.experience.retrieve(p_exp2.world.context)
        if not item.applicable
    ]
    checks.check(
        "软件版本不匹配的案例被判为不适用",
        bool(stale) and any("版本" in item.reason for item in stale),
        f"{[(i.case.case_id, i.reason) for i in stale]}",
    )
    p_exp3 = build_platform(world="env")
    p_exp3.experience = p_exp.experience
    c_exp3 = p_exp3.build_agent().troubleshoot(p_exp3.world.context, use_templates=False)
    checks.check(
        "经验复用没有破坏结论（依然定位到 H_ENV）",
        c_exp3.root_cause_key == "H_ENV",
        f"根因={c_exp3.root_cause_key} 调用={c_exp3.metrics['tool_calls']}",
    )

    # ---------------------------------------------------------------- T13
    print("\n[T13] 模式 B：裁剪复用的有效性判据")
    p_trim = build_platform(world="env")
    ctx = p_trim.world.context
    ctx.cached_checks = {
        "environment_state": {
            "observation": Observation.ANOMALY.value,
            "software_version": "V100R020C10",
            "collected_at": BASE_TIME.isoformat(),
        }
    }
    plan, mode = p_trim.planner.build_from_template(
        ctx, template_for(FailureCategory.ENVIRONMENT)
    )
    checks.check(
        "有效期内的缓存被标记为复用",
        mode.value == "TRIMMED"
        and any(node.reused_from for node in plan.nodes),
        f"mode={mode.value}",
    )
    ctx.cached_checks["environment_state"]["software_version"] = "V100R001C00"
    plan2, mode2 = p_trim.planner.build_from_template(
        ctx, template_for(FailureCategory.ENVIRONMENT)
    )
    checks.check(
        "版本不匹配的缓存不被复用",
        mode2.value == "PREDEFINED"
        and all(node.reused_from is None for node in plan2.nodes
                if node.skill == "check_environment_state"),
        f"mode={mode2.value}",
    )

    # ---------------------------------------------------------------- T14
    print("\n[T14] 证据分层：推断必须能指回原始证据")
    p_ev = build_platform(world="env")
    c_ev = p_ev.troubleshoot(use_templates=False)
    raw = p_ev.store.raw_ancestors(c_ev.evidence_ids)
    checks.check(
        "结论引用的证据背后存在原始证据",
        len(raw) > 0,
        f"原始证据锚点={len(raw)}",
    )
    checks.check(
        "所有证据都能溯源（INFERRED/VERIFIED 的 derived_from 非空）",
        all(
            item.derived_from
            for item in p_ev.store.all()
            if item.kind.value in ("inferred", "verified")
        ),
        f"总证据数={len(p_ev.store)}",
    )
    try:
        from .domain.models import Evidence

        Evidence(kind="inferred", source="x", claim="凭空推断", derived_from=[])
        blocked = False
    except ValidationError:
        blocked = True
    checks.check(
        "没有依据的推断写不进证据库",
        blocked,
        "Evidence(kind=inferred, derived_from=[]) 被校验器拒绝",
    )

    # ---------------------------------------------------------------- T15
    print("\n[T15] 启动自检能发现配置炸弹")
    p_self = build_platform(world="env")
    report = check_config(p_self)
    checks.check(
        "默认装配自检通过",
        report.ok,
        f"problems={report.problems}",
    )
    p_self.pool.require("analyze_logs").likelihoods["H_ENV"] = {"ANOMALY": 0.9}
    broken = check_config(p_self)
    checks.check(
        "似然表缺项会被自检抓出来",
        any("缺少似然项" in problem for problem in broken.problems),
        f"problems={broken.problems[:1]}",
    )

    return checks.report()


# ======================================================================
# 场景注册与入口
# ======================================================================
SCENARIOS: dict[str, tuple[str, Callable[[], int | None]]] = {
    "classify": ("故障分类：规则优先 + LLM 补充（§4）", scenario_classify),
    "trace": ("执行轨迹分析：假成功节点（§12 第一问）", scenario_trace),
    "diagnose": ("证据驱动的动态诊断（§5.3 / §6.2）", scenario_diagnose),
    "workflow": ("动态 Workflow 与校验器（§6.2 / §6.3）", scenario_workflow),
    "recover": ("受控恢复：分级 / 审批 / 验证门（§7）", scenario_recover),
    "unresolved": ("证据不足时如实输出 UNRESOLVED（§9）", scenario_unresolved),
    "experience": ("故障经验库：质量门控与复用（§8）", scenario_experience),
    "tests": ("回归测试（带断言）", scenario_tests),
}
ALL_ORDER = [
    "classify", "trace", "diagnose", "workflow",
    "recover", "unresolved", "experience", "tests",
]


def _configure_output(verbose: bool) -> None:
    # Windows 控制台默认不是 UTF-8，排障报告里全是中文，不设这个会变成乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")       # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8")       # type: ignore[union-attr]
    except (AttributeError, ValueError):               # pragma: no cover
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="troubleshoot",
        description="Troubleshooting Agent —— 证据驱动的排障智能体 Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m app.main --list\n"
            "  python -m app.main all\n"
            "  python -m app.main diagnose recover\n"
            "  python -m app.main tests --verbose\n"
        ),
    )
    parser.add_argument("scenarios", nargs="*", default=[], help="要运行的场景名（留空 = all）")
    parser.add_argument("--list", action="store_true", help="列出所有场景后退出")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    args = parser.parse_args(argv)

    if args.list:
        print("可用场景：\n")
        for name in ALL_ORDER:
            print(f"  {name:<12} {SCENARIOS[name][0]}")
        print(f"\n  {'all':<12} 依次运行全部场景")
        return 0

    _configure_output(args.verbose)

    requested = args.scenarios or ["all"]
    if "all" in requested:
        requested = ALL_ORDER

    unknown = [name for name in requested if name not in SCENARIOS]
    if unknown:
        print(f"未知场景：{unknown}\n可用：{ALL_ORDER}", file=sys.stderr)
        return 2

    print("=" * WIDTH)
    print("  Troubleshooting Agent")
    print("  证据驱动、可验证、受控恢复的排障智能体")
    print(f"  待运行场景：{', '.join(requested)}")
    print("=" * WIDTH)

    exit_code = 0
    for name in requested:
        _, runner = SCENARIOS[name]
        try:
            outcome = runner()
            if isinstance(outcome, int) and outcome != 0:
                exit_code = outcome
        except Exception:  # noqa: BLE001 - 演示入口要把失败完整摊开
            print(f"\n场景 {name} 执行失败：", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1

    print()
    print("=" * WIDTH)
    print("  全部场景执行完毕" if exit_code == 0 else "  存在失败场景，请查看上面的输出")
    print("=" * WIDTH)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
