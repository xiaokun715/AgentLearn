"""动态 Workflow 路由 —— 命令行演示入口。

覆盖《第十七章：动态 Workflow 与 Agent 路由设计说明书》§2 ~ §15 的主线：

    python -m app.main --list
    python -m app.main all
    python -m app.main route compose fate
    python -m app.main tests

设计说明：与本书其它 Demo 一致，**没有 HTTP API 层**。本 Demo 要考察的是
「谁来干活」这个决策怎么做出、怎么解释、怎么沉淀成资产，
而不是接口形态。
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable

from .domain.enums import (
    AgentHealth,
    ExecutionMode,
    IntentStatus,
    RouteTarget,
    StepStatus,
    TemplateFate,
    TemplateState,
)
from .domain.models import RunRequest, ValidationReport, WorkflowTemplate
from .domain.policy import INTENT_LIBRARY, ROUTING, smoothed_success
from .factory import Platform, PlatformConfig, build_platform, check_config
from .intent.recognizer import explain as explain_intent
from .workflow.runtime import RuntimeOptions

WIDTH = 84

#: 演示里反复用到的一句「多步意图」
TROUBLE_QUERY = "SRS 测试失败了，帮我定位一下根因"
TREND_QUERY = "最近吞吐趋势怎么样，画个图"
REVIEW_QUERY = "帮我看看这段代码有没有安全问题"


# ======================================================================
# 终端输出
# ======================================================================
def banner(text: str) -> None:
    print()
    print("=" * WIDTH)
    print(f"  {text}")
    print("=" * WIDTH)


def section(index: int, title: str, ref: str = "") -> None:
    print(f"\n── {index}. {title}{'  [' + ref + ']' if ref else ''}")
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


def table(headers, rows, *, indent: int = 5, aligns=None) -> None:
    rows = [[str(cell) for cell in row] for row in rows]
    columns = len(headers)
    aligns = list(aligns or ["l"] * columns)
    widths = [len(str(headers[i])) for i in range(columns)]
    for row in rows:
        for index, cell in enumerate(row[:columns]):
            widths[index] = max(widths[index], len(cell))
    prefix = " " * indent
    head = "  ".join(
        str(headers[i]).ljust(widths[i]) if aligns[i] == "l"
        else str(headers[i]).rjust(widths[i]) for i in range(columns)
    )
    print(f"{prefix}{head}")
    print(f"{prefix}{'-' * len(head)}")
    for row in rows:
        cells = [
            (row[i] if i < len(row) else "").ljust(widths[i]) if aligns[i] == "l"
            else (row[i] if i < len(row) else "").rjust(widths[i])
            for i in range(columns)
        ]
        print(f"{prefix}{'  '.join(cells)}")


def dag_lines(template: WorkflowTemplate) -> list[str]:
    """把模板画成层级缩进的 DAG。"""
    groups = _levels(template)
    lines = []
    for depth, group in enumerate(groups):
        for step_id in group:
            step = template.step_map()[step_id]
            prefix = "   " + "  " * depth + ("└─ " if depth else "▸ ")
            deps = f"  ← {step.depends_on}" if step.depends_on else ""
            lines.append(f"{prefix}{step_id} {step.agent}{deps}")
    return lines


def _levels(template: WorkflowTemplate) -> list[list[str]]:
    remaining = {step.id: set(step.depends_on) for step in template.steps}
    groups: list[list[str]] = []
    while remaining:
        ready = sorted(sid for sid, deps in remaining.items() if not deps)
        if not ready:
            break
        groups.append(ready)
        for sid in ready:
            remaining.pop(sid)
        for deps in remaining.values():
            deps.difference_update(ready)
    return groups


def run_summary(result) -> None:
    info("路由", f"{result.decision.target.value} → {result.decision.chosen_label}")
    info("意图", explain_intent(result.intent))
    info("步骤", f"{len(result.step_results)} 步，全部成功={result.ok}")
    info("耗时", f"关键路径 {result.total_ms}ms（其中编排 {result.compose_ms}ms）")
    if result.context:
        keys = [k for k in result.context if not k.startswith("_")]
        info("黑板产出", ", ".join(keys))
    info("答复", brief(result.answer, 110))


def show_report(report: ValidationReport, *, title: str = "校验") -> None:
    if report.repairs:
        for item in report.repairs:
            info(f"{title}·修复", item)
    if not report.issues:
        info(f"{title}·结论", "通过（无问题）")
        return
    for issue in report.issues:
        flag = {"blocking": "✗", "warning": "!", "info": "·"}.get(issue.severity, "·")
        info(f"{title}·{flag} {issue.code}", brief(issue.message, 100))


# ======================================================================
# 场景一：池子总览（需求 1）
# ======================================================================
def scenario_pool() -> None:
    banner("场景一：Agent Pool —— 多个子 Agent + 已生成的 Workflow 模板（需求 1）")

    platform = build_platform(PlatformConfig(agent_config=None))

    section(1, "子 Agent：能力、数据流契约、成本、风险", "§4.1")
    rows = [
        [spec.name, spec.domain, ",".join(spec.capabilities),
         f"{spec.cost:.1f}", f"{spec.latency_ms}ms", spec.risk.value, spec.health.value]
        for spec in platform.pool.all()
    ]
    table(["子 Agent", "域", "能力", "成本", "时延", "风险", "健康"],
          rows, aligns=["l", "l", "l", "r", "r", "l", "l"])
    step("路由与编排**只看能力标签，不看 Agent 名字**（§4.2）——")
    step("所以重命名一个子 Agent 不需要改任何一条路由规则。")

    section(2, "数据流契约：reads / writes 决定谁能接在谁后面", "§8.3")
    for spec in platform.pool.all()[:6]:
        info(spec.name, f"读 {spec.reads or '[]'} → 写 {spec.writes}")
    step("没有这张表，编排器只能按名字猜顺序，而校验器无法判断数据流是否闭合")

    section(3, "模板库：平台预置的种子（§13.1 冷启动）", "§4.3")
    for template in platform.store.all():
        info(
            f"{template.template_id}  v{template.version}",
            f"{template.name}｜{template.tags.source.value}｜"
            f"跑过 {template.stats.runs} 次，成功率 {template.stats.success_rate:.0%}",
        )
    step("模板库和子 Agent 都在同一个池子里 —— 对路由引擎来说，它们是**同类候选**（§4.3）")

    section(4, "模板长什么样：一条 6 步 DAG", "§8")
    template = platform.store.search("排障")[0]
    for line in dag_lines(template):
        print(f"       {line}")
    info("关键路径", f"{template.estimated_latency_ms(platform.pool)}ms")
    info("朴素求和", f"{sum(platform.pool.latency_of(s.agent) for s in template.steps)}ms")
    step("编排的意义就是让互不依赖的步骤并行 —— 所以耗时必须按**关键路径**算（§10.2）")


# ======================================================================
# 场景二：意图识别
# ======================================================================
def scenario_intent() -> None:
    banner("场景二：意图识别 —— 规则优先，命不中就澄清（§5）")

    platform = build_platform(PlatformConfig(agent_config=None))

    section(1, "规则命中", "§5.1")
    for query in [TROUBLE_QUERY, REVIEW_QUERY, TREND_QUERY]:
        intent = platform.recognizer.recognize(query)
        info(f"「{brief(query, 22)}」", f"{explain_intent(intent)}")
        info("  需要能力", intent.required_capabilities)
    step("意图的产物不是「一个名字」，而是**能力集合 + 步数估计 + 置信度**（§5.2）")

    section(2, "规则不中：LLM 兜底，但要能认出自己不知道", "§5.1")
    intent = platform.recognizer.recognize("帮我处理一下")
    info("结果", explain_intent(intent))
    info("判定依据", brief(intent.rationale, 100))
    warn("0.35 的置信度不是「有点确定」，是「几乎在猜」—— 它会被路由送去澄清")

    section(3, "estimated_steps 是算出来的，不是猜的", "§5.2")
    rows = []
    for spec in INTENT_LIBRARY.values():
        caps = len(set(spec.required_capabilities))
        rows.append([spec.name, str(caps), str(spec.estimated_steps), spec.domain,
                     spec.risk.value])
    table(["意图", "需能力数", "估计步数", "域", "风险"], rows,
          aligns=["l", "r", "r", "l", "l"])
    step("步数 = 覆盖这些能力最少需要几个 Agent。所以「这个意图天然需要编排」")
    step("是一个可以算的**事实**，而不是一句主观判断。")


# ======================================================================
# 场景三：路由引擎
# ======================================================================
def scenario_route() -> None:
    banner("场景三：路由引擎 —— 三条路径用同一把尺子量（§6）")

    platform = build_platform(PlatformConfig(agent_config=None))
    request = RunRequest(query=TROUBLE_QUERY, actor="alice")
    intent = platform.recognizer.recognize(request.query)
    decision = platform.router.route(intent, request)

    section(1, f"意图：{intent.name}（需要 {len(intent.required_capabilities)} 项能力）", "§5.2")
    info("需要的能力", intent.required_capabilities)

    section(2, "候选与评分明细", "§6.3")
    for candidate in sorted(decision.candidates, key=lambda item: -item.score):
        mark = "✓" if candidate.name == decision.chosen else " "
        print(f"     {mark} {candidate.explain()}")
        if candidate.blocked_reason:
            print(f"         ⛔ {candidate.blocked_reason}")

    section(3, "评分公式", "§6.3")
    print("       score = w_cov·覆盖 + w_intent·意图匹配 + w_rel·可靠度 + w_reuse·复用奖励")
    print("             − 成本 − 延迟 − 风险 − 步数")
    info("权重", f"覆盖={ROUTING.w_coverage} 意图={ROUTING.w_intent} "
                 f"可靠={ROUTING.w_reliability} 复用={ROUTING.w_reuse}")
    step("覆盖权重最高 —— **能力覆盖是硬约束**。一个只覆盖 1/6 能力的直连 Agent，")
    step("不该因为「便宜」而赢过一条覆盖 6/6 的模板。")
    step("部分覆盖的直连候选会被直接标为 blocked（§6.3）：")
    step("让它回答一个它没被设计来回答的问题，比明确失败更糟。")

    section(4, "决策", "§6.5")
    info("终点", f"{decision.target.value}")
    info("选中", decision.chosen_label)
    info("理由", brief(decision.rationale, 110))
    if decision.rejected:
        info("被排除", decision.rejected)

    section(5, "同一套评分在不同意图上的结果", "§6.2")
    rows = []
    for query in [TROUBLE_QUERY, REVIEW_QUERY, TREND_QUERY, "查一下 5G 规范怎么规定的",
                  "总结一下上面的内容"]:
        item = platform.recognizer.recognize(query)
        picked = platform.router.route(item, RunRequest(query=query))
        rows.append([brief(query, 22), item.name, picked.target.value,
                     str(len(picked.candidates)), f"{picked.score:.2f}"])
    table(["query", "意图", "终点", "候选数", "得分"], rows,
          aligns=["l", "l", "l", "r", "r"])
    step("没有一堆 if-else，只有一张评分表 —— 这才是「可解释、可调、可回归」（§6.2）")


# ======================================================================
# 场景四：直连 vs 复用模板
# ======================================================================
def scenario_execute() -> None:
    banner("场景四：执行 —— 直连与复用模板（§7.2 / §10）")

    platform = build_platform(PlatformConfig(agent_config=None))

    section(1, "单步意图 → 直连子 Agent", "§6.2")
    result = platform.run(RunRequest(query="总结一下上面的内容", actor="alice"))
    run_summary(result)
    info("模板处置", result.template_fate.value + "（直连不产生新模板）")
    step("直连没有编排成本，也没有轨迹 —— 它适合「一句话就能问完」的事")

    section(2, "多步意图 → 命中已有模板", "§6.4")
    result = platform.run(RunRequest(query=TROUBLE_QUERY, actor="alice"))
    run_summary(result)
    print("\n     执行顺序（同一层的步骤本可并行）：")
    if result.template is not None:
        for index, group in enumerate(_levels(result.template), start=1):
            print(f"       层 {index}: {group}")
    else:
        print(f"       单步执行：{[item.id for item in result.step_results]}")
    step("耗时按关键路径算：并行分支取最大值，不是求和（§10.2）")

    section(3, "三条路径的对比", "§7.2")
    rows = []
    for label, query, mode in [
        ("① 直连", "总结一下上面的内容", ExecutionMode.AUTO),
        ("② 复用模板", TROUBLE_QUERY, ExecutionMode.AUTO),
        ("③ 现场编排", TREND_QUERY, ExecutionMode.FORCE_WORKFLOW),
    ]:
        item = platform.run(RunRequest(query=query, mode=mode, actor="alice"))
        rows.append([
            label, item.decision.target.value, str(len(item.step_results)),
            f"{item.compose_ms}ms", f"{item.total_ms}ms",
            "是" if item.template_fate is TemplateFate.PENDING else "否",
        ])
    table(["路径", "终点", "步数", "编排成本", "总耗时", "产生新模板"], rows,
          aligns=["l", "l", "r", "r", "r", "l"])
    step("编排成本只付一次 —— 现场生成之后再遇到同样的意图，就是「复用模板」那一行")


# ======================================================================
# 场景五：强制 Workflow（需求 3 前半）
# ======================================================================
def scenario_compose() -> None:
    banner("场景五：强制 Workflow —— 先生成模板，再按模板跑（§7 / §8）")

    platform = build_platform(PlatformConfig(agent_config=None))

    section(1, "用户显式选择 workflow 方式", "§7.1")
    request = RunRequest(query=TREND_QUERY, actor="bob",
                         mode=ExecutionMode.FORCE_WORKFLOW, labels=["容量看板"])
    result = platform.run(request)
    info("路由终点", f"{result.decision.target.value}（forced={result.decision.forced}）")
    info("路由理由", brief(result.decision.rationale, 100))
    step("「强制」就是强制：即使池子里有完全匹配的模板也照样现场生成（§7.1）——")
    step("用户选这个模式，通常是想看「现在能编出什么」，或者要做 A/B 对照。")

    section(2, "生成过程：能力 → Agent → 依赖排序", "§8.1")
    for note in platform.app._compose_notes:
        print(f"       · {note}")
    print()
    for line in dag_lines(result.template):
        print(f"     {line}")

    section(3, "校验与修复", "§9")
    report = platform.validator.validate(result.template, intent=result.intent, request=request)
    show_report(report, title="执行前")

    section(4, "执行", "§10")
    run_summary(result)

    section(5, "LLM 提议路径：模型会怎么写错，校验器怎么拦", "§8.2 / §9.1")
    rows = []
    for style, label in [
        ("valid", "正常提议"),
        ("missing_dependency", "漏写依赖"),
        ("cyclic", "写出环"),
        ("phantom_agent", "发明不存在的 Agent"),
        ("side_effect", "偷偷插入写环境的步骤"),
        ("over_budget", "一口气排十几个步骤"),
    ]:
        p = build_platform(PlatformConfig(agent_config=None, plan_style=style))
        intent = p.recognizer.recognize(TREND_QUERY)
        template, notes = p.app.llm_composer.propose(intent)
        if template is None:
            rows.append([label, "✗ 无可用产出", brief("；".join(notes), 44)])
            continue
        rep = p.validator.validate(template, intent=intent,
                                   request=RunRequest(query=TREND_QUERY))
        codes = sorted({i.code for i in rep.blocking}) or ["OK"]
        fate = "✓ 放行" if rep.ok else "✗ 拦下"
        rows.append([label, fate, brief("；".join(notes) or "；".join(codes), 44)])
    table(["模型提议的问题", "结果", "处理 / 拦截"], rows, aligns=["l", "l", "l"])
    step("注意「发明不存在的 Agent」那一行：它在**提议阶段就被过滤掉了**，")
    step("校验器根本没机会看到它 —— 两道防线各管一段（§8.2）")
    ok("模型只输出结构化 steps（没有代码），但依然要过确定性校验（§8.2）")


# ======================================================================
# 场景六：保存还是丢弃（需求 3 后半 + 需求 4）
# ======================================================================
def scenario_fate() -> None:
    banner("场景六：保存还是丢弃 —— 模板标签与质量门（§11 / §12）")

    platform = build_platform(PlatformConfig(agent_config=None))

    section(1, "现场生成后，模板处于 PENDING", "§12.4")
    result = platform.run(RunRequest(query=TREND_QUERY, actor="carol",
                                     mode=ExecutionMode.FORCE_WORKFLOW))
    info("模板", f"{result.template.template_id}  {result.template.name}")
    info("处置状态", result.template_fate.value)
    step("默认**不落库**：让系统自动保存每一次生成的模板，一周之后库里就是")
    step("一堆同义、低质、没人敢用的 DAG（§12.4）。")

    section(2, "标签：谁生成的、什么时候、从哪来（需求 4）", "§11")
    tags = result.template.tags
    info("生成人 author", tags.author)
    info("生成时间 created_at", tags.created_at.isoformat(timespec="seconds"))
    info("来源 source", tags.source.value)
    info("域 / 意图", f"{tags.domain} / {tags.intent}")
    info("风险", tags.risk.value)
    info("自定义标签", tags.labels)
    info("溯源 query", brief(tags.origin_query, 80))
    step("标签不只是元数据 —— 它是**模板库能被治理的唯一原因**（§11.4）：")
    step("没有「谁生成的、跑过几次、成功率多少」，库就只是一个会把相似 DAG 存一百遍的垃圾场。")

    section(3, "质量门：能不能入库", "§12.1")
    report = platform.validator.validate(result.template, intent=result.intent,
                                         for_saving=True)
    show_report(report, title="保存前")
    info("已成功运行次数", f"{result.template.stats.successes}")
    step("门的第一条就是「必须成功跑过一次」—— 生成者不该自己决定成果够不够格。")

    section(4, "用户选择：保存", "§12")
    outcome = platform.decide_fate(
        result, TemplateFate.SAVE, actor="carol", labels=["容量看板", "团队B"],
    )
    info("入库结果", f"stored={outcome.stored}  {outcome.decision}")
    info("最终标签", result.template.tags.labels)
    info("最终状态", result.template.tags.state.value)

    section(5, "另一个用户选择：丢弃", "§12")
    other = platform.run(RunRequest(query=REVIEW_QUERY, actor="dave",
                                    mode=ExecutionMode.FORCE_WORKFLOW))
    before = len(platform.store.all())
    platform.decide_fate(other, TemplateFate.DISCARD)
    after = len(platform.store.all())
    info("丢弃前库容量", before)
    info("丢弃后库容量", after)
    info("模板状态", other.template.tags.state.value)
    ok("丢弃是显式动作，而且**有记录**（状态标为 BROKEN），不是静默忽略")

    section(6, "第二次相同意图：直接命中刚存的模板", "§6.4")
    again = platform.run(RunRequest(query=TREND_QUERY, actor="carol"))
    info("路由", f"{again.decision.target.value} → {again.decision.chosen_label}")
    info("命中模板", again.decision.chosen)
    info("编排成本", f"{again.compose_ms}ms（第一次是 {result.compose_ms}ms）")
    info("总耗时", f"{again.total_ms}ms（第一次含编排 {result.total_ms}ms）")
    ok("模板库的价值不在「省了一次生成」，而在**把一次偶然的成功变成可复用的资产**")


# ======================================================================
# 场景七：模板库的治理
# ======================================================================
def scenario_library() -> None:
    banner("场景七：模板库 —— 检索、去重、版本、失效（§11 / §13）")

    platform = build_platform(PlatformConfig(agent_config=None))
    # 先造几条现场生成的模板
    for query, actor, labels in [
        (TREND_QUERY, "carol", ["容量看板"]),
        (REVIEW_QUERY, "dave", ["安全审计"]),
    ]:
        item = platform.run(RunRequest(query=query, actor=actor,
                                       mode=ExecutionMode.FORCE_WORKFLOW))
        platform.decide_fate(item, TemplateFate.SAVE, labels=labels)

    section(1, "按意图检索", "§6.4")
    for template in platform.store.by_intent("data_trend"):
        info(template.template_id, f"{template.name}｜{template.tags.author}")

    section(2, "按标签检索（需求 4 的用处）", "§11.4")
    for label in ["容量看板", "安全审计", "排障"]:
        hits = platform.store.by_labels([label])
        info(f"标签 {label}", [t.name for t in hits] or "（无）")
    author_hits = platform.store.by_author("dave")
    info("按生成人 dave", [t.name for t in author_hits] or "（无）")

    section(3, "结构去重：同一份 DAG 不该存两遍", "§12.2")
    again = platform.run(RunRequest(query=TREND_QUERY, actor="erin",
                                    mode=ExecutionMode.FORCE_WORKFLOW))
    outcome = platform.decide_fate(again, TemplateFate.SAVE)
    info("入库结果", f"stored={outcome.stored}  {outcome.decision}")
    info("原因", outcome.reason)
    if outcome.merged_into:
        merged = platform.store.get(outcome.merged_into)
        info("合并后生成人", merged.tags.author)
        info("合并后标签", merged.tags.labels)
    step("不是简单丢弃 —— 同一份 DAG 被两个人分别生成，说明这是**高频需求**，")
    step("这个信号应该体现在已有模板的标签上（生成人变成两个人）。")

    section(4, "版本演进", "§12.3")
    old = platform.store.by_intent("data_trend")[0]
    new = old.model_copy(deep=True)
    new.template_id = "wf_v2_demo"
    new.name = f"{old.name} v2"
    new.steps = [*old.steps, _extra_step(platform, old)]
    new.tags = old.tags.model_copy(update={"state": TemplateState.DRAFT,
                                           "superseded_by": None})
    platform.store.add(new, require_verified=False)
    platform.store.deprecate(old.template_id, new.template_id)
    info("旧版", f"{old.template_id} → {old.tags.state.value}（指向 {old.tags.superseded_by}）")
    info("新版", f"{new.template_id} v{new.version}，基于 {new.based_on}")
    step("旧版**保留**：已经跑过的轨迹还引用着它，删掉会让历史无法复盘")

    section(5, "失效检测：池子变了，模板库必须跟着变", "§13.3")
    victims = [t for t in platform.store.all() if t.tags.state is TemplateState.ACTIVE][:1]
    if victims:
        gone = victims[0].agents_used()[0]
        platform.pool.set_health(gone, AgentHealth.OFFLINE)
        report = platform.store.audit_health()
        info("下线 Agent", gone)
        info("受影响模板", report.broken or "（无）")
        info("库状态分布", report.by_state)
        warn("这是模板库最大的一个坑：Agent 下线了，模板还在引用它。")
        warn("等到有人真的命中才报错 —— 而那时它已经在生产上跑了一半。")
        platform.pool.set_health(gone, AgentHealth.HEALTHY)
        platform.store.audit_health()

    section(6, "库总览", "§11")
    rows = []
    for template in platform.store.all():
        rows.append([
            template.template_id, brief(template.name, 18), template.tags.author,
            template.tags.source.value, template.tags.state.value,
            str(template.stats.runs),
            f"{smoothed_success(template.stats.successes, template.stats.runs):.2f}",
            ",".join(template.tags.labels[:2]),
        ])
    table(["id", "名称", "生成人", "来源", "状态", "跑过", "平滑成功率", "标签"],
          rows, aligns=["l", "l", "l", "l", "l", "r", "r", "l"])
    step("注意「平滑成功率」：跑 1 次成功 1 次的模板，原始成功率是 100%，")
    step("它会立刻压过跑了 42 次成功 39 次的成熟模板 —— 而两者的证据量差了 40 倍。")
    step("拉普拉斯平滑 (s+1)/(n+2) 之后：1/1 → 0.67，39/42 → 0.91（§6.3）")


def _extra_step(platform: Platform, template: WorkflowTemplate):
    from .domain.models import WorkflowStep

    return WorkflowStep(id="gx", agent="knowledge_searcher", depends_on=[],
                        needs=[], produces=["references"],
                        note="v2 新增：先查规范再分析")


# ======================================================================
# 回归测试
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
        passed = sum(1 for _, flag, _ in self.results if flag)
        total = len(self.results)
        print()
        print("=" * WIDTH)
        print(f"  回归测试结果：{passed}/{total} 通过")
        print("=" * WIDTH)
        for name, flag, _ in self.results:
            if not flag:
                print(f"   ✗ FAILED: {name}")
        return 0 if passed == total else 1


def scenario_tests() -> int:
    banner("回归测试 —— 每一条设计结论都有一条对应的断言")
    checks = _Checks()

    def fresh(**kwargs) -> Platform:
        return build_platform(PlatformConfig(agent_config=None, **kwargs))

    # ---------------------------------------------------------------- T1
    print("\n[T1] 池子自检（§15）")
    platform = fresh()
    checks.check("默认装配自检通过", platform.config_report.ok,
                 f"problems={platform.config_report.problems}")
    pool_report = check_config(platform)
    checks.check("能力覆盖完备：每个意图的每项能力都有人提供",
                 pool_report.ok, f"problems={pool_report.problems[:1]}")

    # ---------------------------------------------------------------- T2
    print("\n[T2] 意图识别：规则优先，说不清就 UNKNOWN/AMBIGUOUS（§5）")
    intent = platform.recognizer.recognize(TROUBLE_QUERY)
    checks.check("多步意图走规则命中", intent.decided_by == "rule"
                 and intent.name == "troubleshoot_failure",
                 f"{intent.name} via {intent.decided_by}")
    checks.check("步数估计是结构性的（6 项能力 → 6 步）",
                 intent.estimated_steps == len(set(intent.required_capabilities)),
                 f"caps={len(intent.required_capabilities)} steps={intent.estimated_steps}")
    vague = platform.recognizer.recognize("帮我处理一下")
    checks.check("模糊 query 被标为 AMBIGUOUS 而不是硬猜",
                 vague.status is IntentStatus.AMBIGUOUS,
                 f"{vague.name} conf={vague.confidence}")

    # ---------------------------------------------------------------- T3
    print("\n[T3] 路由：覆盖不足的直连候选必须被排除（§6.3）")
    item = platform.recognizer.recognize(TROUBLE_QUERY)
    decision = platform.router.route(item, RunRequest(query=TROUBLE_QUERY))
    partial = [c for c in decision.candidates
               if c.kind is RouteTarget.DIRECT_AGENT and c.coverage < 1.0]
    checks.check("存在被排除的部分覆盖候选", bool(partial),
                 f"{len(partial)} 个被标记 blocked")
    checks.check("被选中的候选覆盖率为 1.0",
                 all(c.coverage >= 1.0 for c in decision.candidates
                     if c.name == decision.chosen),
                 f"chosen={decision.chosen_label}")
    checks.check("路由决策携带完整候选清单（可解释）",
                 len(decision.candidates) > 1,
                 f"候选 {len(decision.candidates)} 个")

    # ---------------------------------------------------------------- T4
    print("\n[T4] 强制 Workflow：跳过复用，一定现场生成（§7.1）")
    forced = platform.run(RunRequest(query=TROUBLE_QUERY, actor="x",
                                     mode=ExecutionMode.FORCE_WORKFLOW))
    checks.check("终点是 COMPOSE_NEW 且强制标记为真",
                 forced.decision.target is RouteTarget.COMPOSE_NEW and forced.decision.forced,
                 f"{forced.decision.target.value} forced={forced.decision.forced}")
    checks.check("即使存在完全匹配的模板也不复用",
                 bool(platform.store.by_intent("troubleshoot_failure")),
                 "库里有匹配模板，仍走了现场生成")
    checks.check("产生了待处置的新模板",
                 forced.template is not None
                 and forced.template_fate is TemplateFate.PENDING,
                 f"fate={forced.template_fate.value}")
    checks.check("编排成本被计入总耗时",
                 forced.compose_ms > 0 and forced.total_ms > forced.compose_ms,
                 f"compose={forced.compose_ms}ms total={forced.total_ms}ms")

    # ---------------------------------------------------------------- T5
    print("\n[T5] 生成的 DAG 必须是可执行的（§8.3 / §9.1）")
    report = platform.validator.validate(forced.template, intent=forced.intent,
                                         request=forced.request)
    checks.check("校验通过", report.ok, f"blocking={[i.code for i in report.blocking]}")
    checks.check("关键路径耗时 <= 各步之和（证明确实存在并行）",
                 forced.total_ms <= sum(platform.pool.latency_of(s.agent)
                                        for s in forced.template.steps),
                 f"关键路径 {forced.total_ms}ms")

    # ---------------------------------------------------------------- T6
    print("\n[T6] 校验器必须拦下模型写坏的编排（§8.2 / §9.1）")
    for style, expected in [("missing_dependency", "DATA_FLOW_UNORDERED"),
                            ("cyclic", "CYCLE"),
                            ("side_effect", "SIDE_EFFECT_STEP"),
                            ("over_budget", "BUDGET_STEPS")]:
        p = fresh(plan_style=style)
        it = p.recognizer.recognize(TREND_QUERY)
        tpl, _ = p.app.llm_composer.propose(it)
        rep = p.validator.validate(tpl, intent=it,
                                   request=RunRequest(query=TREND_QUERY)) if tpl else None
        codes = {i.code for i in rep.blocking} if rep else set()
        checks.check(f"{style} -> {expected}", expected in codes,
                     f"issues={sorted(codes) or '（模型未产出）'}")

    # ---------------------------------------------------------------- T7
    print("\n[T7] 有副作用的 Agent 不允许被自动编排（§9.1 第 7 项）")
    risky = platform.run(RunRequest(query="修正配置并写回环境", actor="ops"))
    checks.check("意图 config_fix 的编排被拦下",
                 risky.decision.target is RouteTarget.COMPOSE_NEW and risky.template is None,
                 brief(risky.answer, 90))
    checks.check("拦下原因是副作用而不是别的问题",
                 "SIDE_EFFECT_STEP" in " ".join(risky.notes),
                 brief(" ".join(risky.notes), 80))

    # ---------------------------------------------------------------- T8
    print("\n[T8] 保存质量门（§12.1）")
    run = platform.run(RunRequest(query=TREND_QUERY, actor="carol",
                                  mode=ExecutionMode.FORCE_WORKFLOW))
    checks.check("刚生成的模板状态是 DRAFT / PENDING",
                 run.template_fate is TemplateFate.PENDING,
                 f"state={run.template.tags.state.value}")
    checks.check("已跑过一次（入库门的依据）",
                 run.template.stats.successes >= 1,
                 f"successes={run.template.stats.successes}")

    unverified = run.template.model_copy(deep=True)
    unverified.template_id = "wf_unverified"
    unverified.stats = type(unverified.stats)()
    outcome = platform.store.add(unverified, require_verified=True)
    checks.check("没跑过的模板不允许入库",
                 not outcome.stored and outcome.decision == "REJECT_UNVERIFIED",
                 outcome.decision)

    no_author = run.template.model_copy(deep=True)
    no_author.template_id = "wf_no_author"
    no_author.tags = no_author.tags.model_copy(update={"author": ""})
    outcome2 = platform.store.add(no_author, require_verified=False)
    checks.check("缺少生成人标签不允许入库",
                 not outcome2.stored and outcome2.decision == "REJECT_NO_AUTHOR",
                 outcome2.decision)

    # ---------------------------------------------------------------- T9
    print("\n[T9] 标签与检索（需求 4 / §11.4）")
    saved = platform.decide_fate(run, TemplateFate.SAVE, actor="carol",
                                 labels=["容量看板", "团队B"])
    checks.check("带标签入库成功", saved.stored, saved.decision)
    checks.check("生成人被正确记录", run.template.tags.author == "carol",
                 run.template.tags.author)
    checks.check("生成时间已记录", run.template.tags.created_at is not None,
                 str(run.template.tags.created_at))
    checks.check("来源标记为 LLM_GENERATED",
                 run.template.tags.source.value == "LLM_GENERATED",
                 run.template.tags.source.value)
    checks.check("按标签能检索到", len(platform.store.by_labels(["容量看板"])) >= 1,
                 [t.name for t in platform.store.by_labels(["容量看板"])])
    checks.check("按生成人能检索到", len(platform.store.by_author("carol")) >= 1,
                 [t.template_id for t in platform.store.by_author("carol")])

    # ---------------------------------------------------------------- T10
    print("\n[T10] 复用闭环：第二次相同意图命中模板且无编排成本（§6.4）")
    again = platform.run(RunRequest(query=TREND_QUERY, actor="carol"))
    checks.check("路由到已有模板", again.decision.target is RouteTarget.EXISTING_TEMPLATE,
                 f"{again.decision.target.value} → {again.decision.chosen}")
    checks.check("编排成本为 0", again.compose_ms == 0, f"compose_ms={again.compose_ms}")
    checks.check("不产生新模板", again.template_fate is TemplateFate.NONE,
                 again.template_fate.value)
    checks.check("模板统计被更新",
                 platform.store.get(again.decision.chosen).stats.runs >= 2,
                 f"runs={platform.store.get(again.decision.chosen).stats.runs}")

    # ---------------------------------------------------------------- T11
    print("\n[T11] 丢弃是显式且有记录的动作（§12）")
    other = platform.run(RunRequest(query=REVIEW_QUERY, actor="dave",
                                    mode=ExecutionMode.FORCE_WORKFLOW))
    before = len(platform.store.all())
    platform.decide_fate(other, TemplateFate.DISCARD)
    checks.check("丢弃后库容量不变", len(platform.store.all()) == before,
                 f"{before} → {len(platform.store.all())}")
    checks.check("被丢弃的模板状态被标记（不是静默忽略）",
                 other.template.tags.state is TemplateState.BROKEN,
                 other.template.tags.state.value)

    # ---------------------------------------------------------------- T12
    print("\n[T12] 结构去重：同一份 DAG 不存两遍（§12.2）")
    p2 = fresh()
    first = p2.run(RunRequest(query=TREND_QUERY, actor="u1",
                              mode=ExecutionMode.FORCE_WORKFLOW))
    p2.decide_fate(first, TemplateFate.SAVE, labels=["A"])
    second = p2.run(RunRequest(query=TREND_QUERY, actor="u2",
                               mode=ExecutionMode.FORCE_WORKFLOW))
    outcome3 = p2.decide_fate(second, TemplateFate.SAVE, labels=["B"])
    checks.check("第二次入库被判为重复",
                 not outcome3.stored and outcome3.decision == "REJECT_DUPLICATE",
                 f"{outcome3.decision} 合并到 {outcome3.merged_into}")
    checks.check("重复不等于丢弃：标签被合并进已有模板",
                 outcome3.merged_into is not None
                 and "u2" in p2.store.get(outcome3.merged_into).tags.author,
                 p2.store.get(outcome3.merged_into).tags.author if outcome3.merged_into else "-")

    # ---------------------------------------------------------------- T13
    print("\n[T13] 失效检测：Agent 下线后模板不能再被路由（§13.3）")
    p3 = fresh()
    template = p3.store.by_intent("troubleshoot_failure")[0]
    victim = template.agents_used()[0]
    p3.pool.set_health(victim, AgentHealth.OFFLINE)
    report3 = p3.store.audit_health()
    checks.check("引用下线 Agent 的模板被标记 BROKEN",
                 template.template_id in report3.broken,
                 f"broken={report3.broken}")
    item3 = p3.recognizer.recognize(TROUBLE_QUERY)
    decision3 = p3.router.route(item3, RunRequest(query=TROUBLE_QUERY))
    checks.check("路由不再选中它",
                 decision3.chosen != template.template_id,
                 f"选中 {decision3.chosen_label}")

    # ---------------------------------------------------------------- T14
    print("\n[T14] 平滑成功率：别让一次运气压过四十次证据（§6.3）")
    checks.check("1 次成功 1 次 < 39 次成功 42 次",
                 smoothed_success(1, 1) < smoothed_success(39, 42),
                 f"{smoothed_success(1, 1):.3f} < {smoothed_success(39, 42):.3f}")
    checks.check("1 次成功的原始成功率是 100%（这就是问题所在）",
                 1 / 1 == 1.0 and smoothed_success(1, 1) < 0.7,
                 "平滑后 0.667")

    # ---------------------------------------------------------------- T15
    print("\n[T15] 执行器的失败语义：可选步骤跳过，必选步骤中断（§10.3）")
    p5 = fresh()
    intent5 = p5.recognizer.recognize(TROUBLE_QUERY)
    tpl5 = p5.store.by_intent("troubleshoot_failure")[0]
    trace = p5.runtime.execute(tpl5, intent5,
                               RuntimeOptions(fail_steps={"s2": 99}))
    failed = [r for r in trace.results if r.status is StepStatus.FAILED]
    checks.check("必选步骤失败导致下游被阻塞",
                 bool(failed) and any(r.status is StepStatus.BLOCKED for r in trace.results),
                 f"failed={[r.id for r in failed]}")

    tpl6 = tpl5.model_copy(deep=True)
    for step_item in tpl6.steps:
        if step_item.id == "s4":
            step_item.optional = True
    trace6 = p5.runtime.execute(tpl6, intent5, RuntimeOptions(fail_steps={"s4": 99}))
    skipped = [r for r in trace6.results if r.status is StepStatus.SKIPPED]
    checks.check("可选步骤失败只跳过，不阻断整条链",
                 bool(skipped) and trace6.ok,
                 f"skipped={[r.id for r in skipped]}")

    return checks.report()


# ======================================================================
# 场景注册与入口
# ======================================================================
SCENARIOS: dict[str, tuple[str, Callable[[], int | None]]] = {
    "pool": ("池子总览：子 Agent + 模板库（需求 1）", scenario_pool),
    "intent": ("意图识别：规则优先，命不中就澄清（§5）", scenario_intent),
    "route": ("路由引擎：三条路径同一把尺子（§6）", scenario_route),
    "execute": ("执行：直连 vs 复用模板（§7.2 / §10）", scenario_execute),
    "compose": ("强制 Workflow：先生成再执行（§7 / §8）", scenario_compose),
    "fate": ("保存还是丢弃：标签与质量门（§11 / §12）", scenario_fate),
    "library": ("模板库治理：检索 / 去重 / 版本 / 失效（§13）", scenario_library),
    "tests": ("回归测试（带断言）", scenario_tests),
}
ALL_ORDER = ["pool", "intent", "route", "execute", "compose", "fate", "library", "tests"]


def _configure_output(verbose: bool) -> None:
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
        prog="workflow-router",
        description="Dynamic Workflow Router —— 意图识别 / 路由 / 现场编排 / 模板库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m app.main --list\n"
            "  python -m app.main all\n"
            "  python -m app.main route compose\n"
            "  python -m app.main tests\n"
        ),
    )
    parser.add_argument("scenarios", nargs="*", default=[], help="要运行的场景名（留空 = all）")
    parser.add_argument("--list", action="store_true", help="列出所有场景后退出")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    args = parser.parse_args(argv)

    if args.list:
        print("可用场景：\n")
        for name in ALL_ORDER:
            print(f"  {name:<10} {SCENARIOS[name][0]}")
        print(f"\n  {'all':<10} 依次运行全部场景")
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
    print("  Dynamic Workflow Router")
    print("  意图识别 → 路由引擎 → 子 Agent / 已有模板 / 现场编排")
    print(f"  待运行场景：{', '.join(requested)}")
    print("=" * WIDTH)

    exit_code = 0
    for name in requested:
        _, runner = SCENARIOS[name]
        try:
            outcome = runner()
            if isinstance(outcome, int) and outcome != 0:
                exit_code = outcome
        except Exception:  # noqa: BLE001
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
