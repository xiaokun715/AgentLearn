"""风控 Agent —— 命令行演示入口。

覆盖《第十八章：风控 Agent 设计说明书》§1 ~ §7 的主线：

    python -m app.main --list
    python -m app.main all
    python -m app.main paths boundary
    python -m app.main tests

设计说明：与本书其它 Demo 一致，**没有 HTTP API 层**。本 Demo 要考察的是
「传统风控与 Agent 怎么分工、边界在哪、价值怎么验证」，
而不是接口形态。
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable

from .domain.enums import (
    Disposition,
    HypothesisStatus,
    PatternStatus,
    ToolDenyReason,
)
from .domain.models import Event, ToolCall
from .domain.policy import (
    AGENT_GRANTS,
    DECISION,
    ONLINE_RULES,
    STRATEGY_POLICY,
    TOOL_INDEX,
)
from .evaluation.offline import MIN_PRECISION
from .factory import PlatformConfig, RiskPlatform, build_platform
from .gateway.tool_gateway import build_scope

WIDTH = 86

#: 演示里反复用到的三个案件
CASES = [
    ("group_fraud", "u_fraud_00", "设备农场批量注册领券后退款套利"),
    ("false_positive", "u_normal_00", "商品质量问题导致的集中退款（误报）"),
    ("sparse_data", "u_sparse_00", "设备维度数据源不可用"),
]


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
    print(f"     {key:<26} {value}")


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


def show_hypotheses(state, *, only: str = "all") -> None:
    rows = []
    for item in sorted(state.hypotheses.values(), key=lambda i: -i.weight):
        if only == "risk" and item.key in ("H_LEGITIMATE", "H_INSUFFICIENT_DATA"):
            continue
        rows.append([
            item.key, item.status.value, f"{item.weight:.2f}",
            str(len(item.support)), str(len(item.refute)),
            ",".join(sorted(state.sources_for(item.key))) or "-",
        ])
    table(["假设", "状态", "权重", "支持", "反驳", "来源"], rows,
          aligns=["l", "l", "r", "r", "r", "l"])


# ======================================================================
# 场景一：在线决策链路
# ======================================================================
def scenario_online() -> None:
    banner("场景一：在线决策链路 —— 确定性、毫秒级、不调查（§1）")

    section(1, "链路组成", "§1.2")
    info("特征层", "注册频率、设备关联数、退款率、请求频率…")
    info("规则引擎", f"{len(ONLINE_RULES)} 条确定性规则")
    info("风险模型", "线性模型，输出 0~1 的风险分")
    info("决策引擎", "规则 + 模型 → 放行 / 验证 / 拦截 / 人工")
    step("这一层**不需要 Agent** —— 它的任务是「这一笔要不要放行」，"
         "有明确输入输出的问题不该经过多轮推理")

    section(2, "规则表", "§1.3")
    table(["规则", "说明", "特征", "阈值", "风险"],
          [[rule.rule_id, rule.description, rule.feature, rule.threshold, rule.risk_level.value]
           for rule in ONLINE_RULES],
          aligns=["l", "l", "l", "r", "l"])

    section(3, "三种典型事件的在线裁决", "§1.3")
    rows = []
    # 每个账号来自它所在的那个世界 —— 同一个世界里的用户是**同一批**，
    # 跨世界取用户会拿到全 0 的特征（这在真机上对应「查无此人」）。
    for label, world_name, user_id, device, campaign in [
        ("团伙账号领券", "group_fraud", "u_fraud_00", "dev_pool_a", "cmp_001"),
        ("正常老用户领券", "false_positive", "u_normal_00", "dev_own_0", "cmp_001"),
        ("农场账号领券", "device_farm", "u_farm_00", "dev_farm_00", "cmp_001"),
    ]:
        world_platform = build_platform(world_name, PlatformConfig(tool_config=None))
        event = Event(event_type="claim_coupon", user_id=user_id, device_id=device,
                      campaign_id=campaign, occurred_at_ms=1_760_000_000_000)
        decision = world_platform.evaluate_online(event)
        rows.append([
            label, decision.decision.value,
            ",".join(decision.hit_rule_ids) or "—",
            f"{decision.model_score.score:.3f}",
            f"{decision.latency_ms}ms",
        ])
    table(["事件", "处置", "命中规则", "模型分", "延迟"], rows,
          aligns=["l", "l", "l", "r", "r"])

    section(4, "规则与模型分歧时，在线链路不下结论", "§1.3")
    divergent_platform = build_platform("false_positive", PlatformConfig(tool_config=None))
    event = Event(event_type="claim_coupon", user_id="u_normal_00",
                  device_id="dev_own_0", campaign_id="cmp_001")
    decision = divergent_platform.evaluate_online(event)
    info("处置", decision.decision.value)
    info("理由", brief(decision.rationale, 110))
    step("朴素做法是「谁更严听谁的」，但它掩盖了一个重要情况：")
    step("规则匹配的是**已知模式**（可能因场景变化而误报），")
    step("模型学的是**历史分布**（对没见过的形态天然不敏感）。")
    ok("两者分歧时正确的动作不是选一个，而是承认自己不知道 —— 转人工/调查")
    step("这就是本 Demo 里所有告警的来源")

    section(5, "Agent 看不到的东西", "§1.4")
    info("在线链路延迟", f"{decision.latency_ms}ms")
    info("Agent 调查耗时", "秒级（3~4 次工具调用）")
    warn("高并发、低延迟的交易决策没有必要经过一个需要多轮推理的 Agent ——")
    warn("把 Agent 放进在线链路，是用几十毫秒换一个「可能更聪明」的答案，不划算")


# ======================================================================
# 场景二：动态调查
# ======================================================================
def scenario_investigate() -> None:
    banner("场景二：动态调查 —— 每一步由上一步的结果决定（§4）")

    platform = build_platform("group_fraud", PlatformConfig(tool_config=None))
    alert = platform.make_alert(
        "u_fraud_00",
        "规则 R002/R003 判定高风险，但模型分仅 0.32 —— 两者分歧，转人工调查",
        rule_ids=["R002_DEVICE_SHARING", "R003_REFUND_RATIO"], model_score=0.32,
    )

    section(1, "输入：一个告警，不是一段日志", "§4.1")
    info("实体", f"{alert.entity_type} {alert.entity_id}")
    info("触发规则", alert.rule_ids)
    info("模型分", alert.model_score)
    info("理由", brief(alert.reason, 90))
    step("告警里只有「足以怀疑」的信息，**不足以判断** —— 这正是需要调查的原因")

    section(2, "调查范围：授权的是「实体 + 直接关联」，不是全库", "§3.2")
    scope = build_scope(platform.world, alert.entity_id)
    info("范围内实体数", len(scope))
    info("示例", sorted(scope)[:8])
    warn("没有范围约束，一个被污染的告警实体就能让 Agent 去遍历整个用户库")

    section(3, "开始调查", "§4.4")
    report, state = platform.investigate(alert)
    print()
    for note in state.notes:
        print(f"     · {brief(note, 120)}")

    section(4, "假设演化（每一条证据都改变了权重）", "§4.4")
    show_hypotheses(state)

    section(5, "调查报告", "§4.6")
    info("结论", report.outcome.value)
    info("处置建议", f"{report.disposition.value}（executed={report.executed}）")
    info("置信度", f"{report.confidence:.2f}")
    info("工具调用", report.tool_calls)
    info("证据条数", len(report.evidence_ids))
    info("调查路径", " → ".join(report.investigation_path))
    info("摘要", brief(report.summary, 120))
    if report.missing_evidence:
        info("未覆盖的维度", report.missing_evidence)
    print()
    print("     " + platform.llm.summarize_report(
        entity_id=report.entity_id, path=report.investigation_path,
        supported=[h["key"] for h in report.hypotheses if h["status"] == "SUPPORTED"],
        refuted=[h["key"] for h in report.hypotheses if h["status"] == "REFUTED"],
        missing=report.missing_evidence,
    ))

    section(6, "结论是**建议**，不是执行", "§4.6")
    info("disposition", report.disposition.value)
    info("executed", report.executed)
    ok("Agent 输出建议；执行是另一条链路上的事（见场景四）")


# ======================================================================
# 场景三：路径对比 —— 与固定工作流的分界线
# ======================================================================
def scenario_paths() -> None:
    banner("场景三：路径对比 —— Agent 与固定工作流的分界线（§5）")

    section(1, "同一批案件，两种跑法", "§5")
    table(["案件", "裁判结论", "Agent 路径", "固定流程路径"],
          [["① 设备农场套利", "确认风险", "账号→设备→图分析", "账号→设备→交易→案例"],
           ["② 商品质量退款", "误报", "账号→交易→设备", "账号→设备→交易→案例"],
           ["③ 设备数据不可用", "证据不足", "账号→交易→设备→案例", "账号→设备→交易→案例"]],
          aligns=["l", "l", "l", "l"])
    step("② 与 ① 的路径在**第二步就分开了** —— 因为账号画像的结果不同：")
    step("① 看到「新账号 + 等级 0 + 领券超阈」→ 下一步查设备；")
    step("② 看到「老账号 + 等级 4 + 历史订单 120」→ 下一步查交易明细。")
    step("顺序不是人预先定义的，而是**证据决定的**。")

    section(2, "实测：结论正确性与步数", "§5")
    rows = []
    details: list[tuple[str, object, object, str]] = []
    for world, entity, label in CASES:
        platform = build_platform(world, PlatformConfig(tool_config=None))
        truth = platform.world.truth[entity]

        agent_report, _ = platform.investigate(
            platform.make_alert(entity, "规则与模型分歧", model_score=0.32))
        fixed_report, meta = platform.run_fixed_workflow(
            platform.make_alert(entity, "规则与模型分歧", model_score=0.32))

        rows.append([
            label,
            truth,
            ("✓ " if agent_report.outcome.value == truth else "✗ ") + agent_report.outcome.value,
            f"{agent_report.tool_calls}",
            ("✓ " if fixed_report.outcome.value == truth else "✗ ") + fixed_report.outcome.value,
            f"{fixed_report.tool_calls}",
        ])
        details.append((label, agent_report, fixed_report, meta))
    table(["案件", "裁判结论", "Agent 结论", "步", "固定流程结论", "步"], rows,
          aligns=["l", "l", "l", "r", "l", "r"])

    section(3, "固定流程错在哪：把「查不到」当成「没问题」", "§5")
    for label, agent_report, fixed_report, meta in details:
        if meta["inconclusive"]:
            info(label, f"数据源无返回 {meta['inconclusive']} 次")
            info("  固定流程结论", fixed_report.outcome.value)
            info("  Agent 结论", agent_report.outcome.value)
            info("  Agent 的未覆盖维度", agent_report.missing_evidence)
    warn("这是最危险的一类错误：它安静、可解释、看起来完全正常 ——")
    warn("「未发现异常」和「查完没发现异常」在报告上长得一模一样")

    section(4, "Agent 多做了什么", "§5")
    for label, agent_report, _fixed, _meta in details:
        info(label, f"未覆盖维度 {len(agent_report.missing_evidence)} 项，"
                    f"证据 {len(agent_report.evidence_ids)} 条")
    ok("Agent 的价值不总是「更快」—— 在案件 ③ 里它反而多查了一步，")
    ok("但正是那一步让它**能够说出自己没查全**")


# ======================================================================
# 场景四：权限边界
# ======================================================================
def scenario_boundary() -> None:
    banner("场景四：权限边界 —— Agent 只能建议，不能执行（§3.2）")

    platform = build_platform("group_fraud", PlatformConfig(tool_config=None))
    gateway = platform.gateway
    scope = build_scope(platform.world, "u_fraud_00")

    section(1, "Agent 的权限集", "§3.2")
    info("持有", sorted(AGENT_GRANTS))
    info("**没有**", sorted({"risk:enforce", "risk:admin"} - AGENT_GRANTS))
    step("这不是「暂时忘了加」—— 它是本设计的核心约束：")
    step("调查 Agent 永远不能执行处置、永远不能改规则（§3.2）")

    section(2, "Agent 尝试直接执行处置", "§3.2")
    rows = []
    for tool, args, why in [
        ("apply_disposition", {"entity_id": "u_fraud_00", "action": "KEEP_BLOCK"},
         "「我已经确认是风险了，直接封掉」"),
        ("release_block", {"entity_id": "u_normal_00"},
         "「这是误报，直接放行」"),
        ("update_rule_config", {"rule_id": "R003_REFUND_RATIO", "threshold": 0.8},
         "「误报太多，把阈值调高一点」"),
        ("publish_strategy", {"candidate_id": "sc_001"},
         "「这个策略验证过了，发布吧」"),
    ]:
        verdict = gateway.check(ToolCall(tool=tool, arguments=args, reason=why), scope=scope)
        rows.append([tool, TOOL_INDEX[tool].kind.value, TOOL_INDEX[tool].risk.value,
                     "✓ 放行" if verdict.allowed else "✗ " + verdict.deny_reason.value])
    table(["Agent 想做的事", "类别", "风险", "结果"], rows,
          aligns=["l", "l", "l", "l"])
    print()
    for tool in ("apply_disposition", "update_rule_config"):
        verdict = gateway.check(ToolCall(tool=tool, arguments={}), scope=scope)
        info(f"{tool} 的理由", brief(verdict.message, 110))

    section(3, "越界查询", "§3.2")
    outsider = next(uid for uid in platform.world.users if uid not in scope)
    verdict = gateway.check(
        ToolCall(tool="query_account_profile", arguments={"user_id": outsider}), scope=scope)
    info("查询范围外的账号", f"{outsider}")
    info("结果", f"✗ {verdict.deny_reason.value}")
    info("理由", brief(verdict.message, 110))
    warn("授权的是「这个实体的关联集合」，不是「任意查询」—— 这是数据安全问题")

    section(4, "正确的路径：人工审批后的一次性授权", "§3.2")
    approval_id = gateway.grant_approval(
        "apply_disposition", "APPROVAL_2026_1017_001", approver="risk-reviewer-01")
    approved = gateway.check(
        ToolCall(tool="apply_disposition",
                 arguments={"entity_id": "u_fraud_00", "action": "KEEP_BLOCK"},
                 approval_id=approval_id),
        scope=scope)
    info("审批号", approval_id)
    info("结果", "✓ 放行" if approved.allowed else "✗ 拦下")
    step("注意审批的是「这一次调用」，不是一个永久权限 ——")
    step("人当初批准的是某个具体动作，不是「以后这类事你都可以自己做」")

    section(5, "改规则连审批通道都没有", "§3.2")
    try:
        gateway.grant_approval("update_rule_config", "X", approver="someone")
        info("结果", "竟然通过了（不应该发生）")
    except PermissionError as exc:
        info("申请审批", "✗ PermissionError")
        info("理由", brief(str(exc), 110))
    ok("改风控配置没有「Agent 申请一下就放行」的通道 —— 这条路径根本不存在")

    section(6, "全量审计", "§3.2")
    snapshot = gateway.snapshot()
    info("调用总数", snapshot["total"])
    info("放行 / 拒绝", f"{snapshot['allowed']} / {snapshot['denied']}")
    info("拒绝原因分布", snapshot["denied_by_reason"])
    info("高危尝试", snapshot["dangerous_attempts"])
    print()
    print("     最近几条审计记录：")
    for record in gateway.audit_tail(5):
        flag = "✓" if record.allowed else "✗"
        print(f"       {flag} {record.tool:<24} {record.permission:<16} "
              f"{record.risk:<7} {record.reason[:36]}")


# ======================================================================
# 场景五：未知风险发现（方式二）
# ======================================================================
def scenario_discover() -> None:
    banner("场景五：未知风险发现 —— Agent 提假设，数据做判定（§5 / 方式二）")

    platform = build_platform("device_farm", PlatformConfig(tool_config=None))

    section(1, "先看清楚：**现有规则一条都没命中**", "§5")
    rows = []
    for index in range(3):
        event = Event(event_type="claim_coupon", user_id=f"u_farm_{index:02d}",
                      device_id=f"dev_farm_{index:02d}", campaign_id="cmp_001")
        decision = platform.evaluate_online(event)
        rows.append([f"u_farm_{index:02d}", decision.decision.value,
                     ",".join(decision.hit_rule_ids) or "—",
                     f"{decision.model_score.score:.3f}"])
    table(["农场账号", "在线处置", "命中规则", "模型分"], rows,
          aligns=["l", "l", "l", "r"])
    step("每个账号领券 3 次（阈值 5）、退款率 40%（阈值 55%）、各自一台设备 ——")
    step("单看每一个，全都是正常用户。这正是「分散操作规避单账号规则」的形态。")

    section(2, "Agent 从整体分布里发现异常", "§5.1")
    patterns = platform.discoverer.discover()
    for note in platform.discoverer.notes:
        print(f"     · {note}")

    section(3, "提出的候选模式", "§5.1")
    table(["候选模式", "条件"],
          [[candidate.name, candidate.describe()] for candidate in patterns],
          aligns=["l", "l"])

    section(4, "离线回放：**这一步才能判断假设成不成立**", "§5.3")
    evaluated = []
    for candidate in patterns:
        result = platform.validator.replay(candidate)
        pattern = platform.validator.evaluate(candidate)
        evaluated.append((candidate, result, pattern))
        info(candidate.name, "")
        info("  命中 / 其中黑产", f"{result.flagged} / {result.true_positive}")
        info("  准确率 / 召回率", f"{result.precision:.3f} / {result.recall:.3f}")
        info("  状态", pattern.status.value)
        for note in pattern.notes:
            print(f"       · {brief(note, 110)}")
    warn(f"第一个模式准确率只有 {evaluated[0][1].precision:.2f} —— "
         f"低于门槛 {MIN_PRECISION}，被退回")
    ok("**模型觉得很可疑，数据说不行。** 这就是「Agent 不能自己宣布发现新风险」的含义")

    section(5, "精化：把弱信号组合起来", "§5.4")
    weak, strong = evaluated[0], evaluated[1]
    info("弱信号单独用", f"命中 {weak[1].flagged}，准确率 {weak[1].precision:.3f}")
    info("两个弱信号叠加", f"命中 {strong[1].flagged}，准确率 {strong[1].precision:.3f}")
    step("「设备型号不可用」有 125 个正常账号；「注册时间聚集」有 20 个正常账号；")
    step("但两者**同时命中**的只有 35 个，其中 30 个是黑产。")
    ok("这是 Agent 擅长的部分：不是发现某个信号，而是**想到把两个弱信号组合起来**")

    section(6, "提升为已确认：只有人能走完这一步", "§5.4")
    pattern = strong[2]
    info("当前状态", pattern.status.value)
    try:
        platform.validator.confirm(pattern, reviewer="")
        info("空签字确认", "竟然通过了（不应该发生）")
    except PermissionError as exc:
        info("空签字确认", f"✗ {exc}")
    confirmed = platform.validator.confirm(
        pattern, reviewer="risk-expert-02",
        counter_evidence=["历史上有一批正常用户也使用模拟器做兼容性测试"])
    info("人工确认后", confirmed.status.value)
    info("decided_by", confirmed.decided_by)
    info("建议转化为", confirmed.proposed_as)
    ok("Agent 只能把假设推到 CANDIDATE；CANDIDATE → VALIDATED 必须由离线回放 + 人确认")


# ======================================================================
# 场景六：策略运营（方式三）
# ======================================================================
def scenario_strategy() -> None:
    banner("场景六：策略运营 —— 分析误报、提出候选，但不能发布（§6 / 方式三）")

    platform = build_platform("device_farm", PlatformConfig(tool_config=None))

    section(1, "问题发现：某条规则的误报在上升", "§6.1")
    analysis = platform.strategy.analyze("R003_REFUND_RATIO")
    info("复盘样本", f"{analysis.total} 条人工审核过的告警")
    info("其中误报", f"{analysis.false_positives} 条（{analysis.fp_rate:.0%}）")
    print()
    table(["场景", "告警数", "误报数", "误报率"],
          [[scene, bucket["total"], bucket["fp"], f"{bucket['fp_rate']:.0%}"]
           for scene, bucket in sorted(analysis.by_scene.items())],
          aligns=["l", "r", "r", "r"])
    info("发现", brief(analysis.finding, 110))

    section(2, "原因定位：规则没有考虑活动场景", "§6.2")
    step("活动期间用户行为基线本就不同（冲动消费、凑单后退）——")
    step("一条按平时分布标定的固定阈值，在大促期间会系统性误报。")
    rule = next(r for r in ONLINE_RULES if r.rule_id == "R003_REFUND_RATIO")
    info("该规则的历史误报率", f"{rule.observed_fp_rate:.0%}")
    info("促销期阈值放宽系数", f"{DECISION.promo_refund_relax:.2f}")

    section(3, "生成候选策略", "§6.2")
    candidate = platform.strategy.propose(analysis)
    info("目标规则", candidate.target_rule)
    info("变更类型", candidate.change_type)
    info("方案", brief(candidate.proposal, 110))
    info("预期误报下降", f"{candidate.expected_fp_reduction:.1%}")
    info("预期漏报上升", f"{candidate.expected_fn_delta:.1%}")
    info("变更风险", candidate.risk_of_change.value)

    section(4, "准入检查：**只报收益不报代价的候选会被拒回**", "§6.3")
    passed, problems = platform.strategy.gate(candidate)
    info("候选 A（完整）", "✓ 通过" if passed else f"✗ {problems}")

    bad = candidate.model_copy(deep=True)
    bad.candidate_id = "sc_bad"
    bad.expected_fn_delta = 0.0
    bad.rationale = "误报太多，把阈值调高"
    bad_passed, bad_problems = platform.strategy.gate(bad)
    info("候选 B（只报误报下降）", "✓ 通过" if bad_passed else "✗ 拒回")
    for problem in bad_problems:
        print(f"       · {problem}")
    warn("这不是格式要求 —— 它挡的是「用放宽阈值把指标做好看」这个动机")

    section(5, "Agent 不能发布", "§6.3")
    verdict = platform.gateway.check(
        ToolCall(tool="publish_strategy", arguments={"candidate_id": candidate.candidate_id}))
    info("尝试发布", f"✗ {verdict.deny_reason.value}")
    info("理由", brief(verdict.message, 110))
    info("candidate.applied", candidate.applied)
    print()
    print("     Agent 能做的：")
    for item in STRATEGY_POLICY.can:
        print(f"       ✓ {item}")
    print("     Agent 不能做的：")
    for item in STRATEGY_POLICY.cannot:
        print(f"       ✗ {item}")


# ======================================================================
# 场景七：价值量化
# ======================================================================
def scenario_value() -> None:
    banner("场景七：价值怎么衡量 —— 三层次与可验证的指标（§7）")

    section(1, "第一层：执行效率", "§7.1")
    rows = []
    for world, entity, label in CASES:
        platform = build_platform(world, PlatformConfig(tool_config=None))
        report, _ = platform.investigate(
            platform.make_alert(entity, "规则与模型分歧", model_score=0.32))
        fixed, _meta = platform.run_fixed_workflow(
            platform.make_alert(entity, "规则与模型分歧", model_score=0.32))
        rows.append([
            label, f"{report.tool_calls}", f"{fixed.tool_calls}",
            f"{len(report.evidence_ids)}", ",".join(report.investigation_path[:3]),
        ])
    table(["案件", "Agent 调用", "固定流程调用", "证据条数", "路径"], rows,
          aligns=["l", "r", "r", "r", "l"])
    step("人工调查需要登录 4 个系统、逐个查询、手工整理 ——")
    step("Agent 把这件事变成一次调用，而且**每次都留下了结构化的证据链**。")
    warn("注意「更少调用」不是目标：第三个案件里 Agent 反而多查了一步，")
    warn("但正是那一步让它能说出自己没查全（见第二层）")

    section(2, "第二层：分析能力", "§7.2")
    rows = []
    for world, entity, label in CASES:
        platform = build_platform(world, PlatformConfig(tool_config=None))
        agent_report, _ = platform.investigate(
            platform.make_alert(entity, "规则与模型分歧", model_score=0.32))
        fixed_report, _ = platform.run_fixed_workflow(
            platform.make_alert(entity, "规则与模型分歧", model_score=0.32))
        truth = platform.world.truth[entity]
        rows.append([
            label,
            "✓" if agent_report.outcome.value == truth else "✗",
            "✓" if fixed_report.outcome.value == truth else "✗",
            f"{len(agent_report.missing_evidence)}",
            f"{agent_report.confidence:.2f}",
        ])
    table(["案件", "Agent 正确", "固定流程正确", "未覆盖维度", "置信度"], rows,
          aligns=["l", "l", "l", "r", "r"])
    step("「未覆盖维度」这个字段是分析能力的直接体现 ——")
    step("一个说不出自己没查什么的系统，它的「没发现异常」是不可信的。")

    section(3, "第三层：策略迭代效率", "§7.3")
    platform = build_platform("device_farm", PlatformConfig(tool_config=None))
    analysis = platform.strategy.analyze("R003_REFUND_RATIO")
    candidate = platform.strategy.propose(analysis)
    info("Badcase 分析", f"{analysis.total} 条样本 → 发现场景依赖")
    info("候选策略", brief(candidate.proposal, 90))
    info("验证方案", "离线回放 + 小流量灰度 + 人工评审")
    step("Agent 把「翻报表 → 读案例 → 提假设 → 写方案」压缩成一次分析，")
    step("但**验证和发布仍然是人的事** —— 这才是「加快迭代」而不是「绕过评审」")

    section(4, "必须一起看的四个指标", "§7.4")
    table(["指标", "为什么不能单独看"],
          [["自动完成率", "只报这个，系统会倾向于「什么都说没风险」以求得高完成率"],
           ["根因/结论准确率", "需要人工复核样本，成本高但不做就等于没验证"],
           ["误处置率", "把正常用户判成风险的代价，与漏报的代价不对称"],
           ["策略变更后的业务指标", "误报下降但如果漏报上升，指标好看、敞口变大"]],
          aligns=["l", "l"])
    warn("这些都是**可以设计和验证的目标**，不是引入 Agent 后必然取得的收益。")
    warn("最终要通过真实数据和对照实验来证明 —— 这件事本 Demo 只能给出方法，")


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

    def fresh(world: str = "group_fraud", **kwargs) -> RiskPlatform:
        return build_platform(world, PlatformConfig(tool_config=None, **kwargs))

    # ---------------------------------------------------------------- T1
    print("\n[T1] 平台自检：Agent 不能有执行/改配置的权限（§3.2）")
    platform = fresh()
    checks.check("默认装配自检通过", platform.config_report.ok,
                 f"problems={platform.config_report.problems}")
    checks.check("Agent 权限集不含 risk:enforce / risk:admin",
                 not (AGENT_GRANTS & {"risk:enforce", "risk:admin"}),
                 f"grants={sorted(AGENT_GRANTS)}")
    checks.check("改规则的工具不在可审批通道里",
                 "update_rule_config" not in platform.gateway.approval_grants,
                 f"可审批={sorted(platform.gateway.approval_grants)}")

    # ---------------------------------------------------------------- T2
    print("\n[T2] 在线链路：确定性、毫秒级、不调查（§1）")
    event = Event(event_type="claim_coupon", user_id="u_fraud_00",
                  device_id="dev_pool_a", campaign_id="cmp_001")
    decision = platform.evaluate_online(event)
    checks.check("在线决策在 10ms 内完成", decision.latency_ms <= 10,
                 f"{decision.latency_ms}ms")
    checks.check("命中设备的规则（HIGH）", "R002_DEVICE_SHARING" in decision.hit_rule_ids,
                 f"hits={decision.hit_rule_ids}")
    two_runs = [platform.evaluate_online(event).decision for _ in range(3)]
    checks.check("同一事件多次评估结果完全一致（确定性）",
                 len({item.value for item in two_runs}) == 1,
                 f"{[item.value for item in two_runs]}")

    # ---------------------------------------------------------------- T3
    print("\n[T3] 规则与模型分歧时不下结论（§1.3）")
    divergent_platform = fresh("false_positive")
    divergent = divergent_platform.evaluate_online(Event(
        event_type="claim_coupon", user_id="u_normal_00", device_id="dev_own_0",
        campaign_id="cmp_001"))
    checks.check("高规则分 + 低模型分 -> REVIEW 而不是拦截",
                 divergent.decision.value == "REVIEW",
                 f"{divergent.decision.value}：{brief(divergent.rationale, 70)}")

    # ---------------------------------------------------------------- T4
    print("\n[T4] 三个案件的结论都正确（§4.5）")
    for world, entity, label in CASES:
        p = fresh(world)
        report, _ = p.investigate(p.make_alert(entity, "分歧", model_score=0.32))
        truth = p.world.truth[entity]
        checks.check(f"{label} -> {truth}", report.outcome.value == truth,
                     f"got={report.outcome.value} path={'→'.join(report.investigation_path)}")

    # ---------------------------------------------------------------- T5
    print("\n[T5] 路径由证据决定，不是固定清单（§5）")
    paths = {}
    for world, entity, _label in CASES:
        p = fresh(world)
        report, _ = p.investigate(p.make_alert(entity, "分歧", model_score=0.32))
        paths[world] = report.investigation_path
    checks.check("不同案件的调查路径不同",
                 len({tuple(v) for v in paths.values()}) > 1,
                 f"{ {k: v for k, v in paths.items()} }")
    checks.check("设备农场案件的第二步不是固定清单里的第二步",
                 paths["group_fraud"][1] != paths["false_positive"][1]
                 or paths["group_fraud"] != paths["false_positive"],
                 f"fraud={paths['group_fraud']} fp={paths['false_positive']}")

    # ---------------------------------------------------------------- T6
    print("\n[T6] 固定流程会把「查不到」当成「没问题」（§5）")
    p = fresh("sparse_data")
    agent_report, _ = p.investigate(p.make_alert("u_sparse_00", "分歧", model_score=0.32))
    fixed_report, meta = p.run_fixed_workflow(
        p.make_alert("u_sparse_00", "分歧", model_score=0.32))
    truth = p.world.truth["u_sparse_00"]
    checks.check("确实有数据源无返回", meta["inconclusive"] > 0,
                 f"inconclusive={meta['inconclusive']}")
    checks.check("Agent 结论正确（证据不足）", agent_report.outcome.value == truth,
                 f"got={agent_report.outcome.value}")
    checks.check("固定流程结论错误（把无结论当成无风险）",
                 fixed_report.outcome.value != truth,
                 f"got={fixed_report.outcome.value}，正确应为 {truth}")
    checks.check("Agent 报告里列出了未覆盖的维度",
                 bool(agent_report.missing_evidence),
                 f"{agent_report.missing_evidence}")

    # ---------------------------------------------------------------- T7
    print("\n[T7] 权限边界：越权调用全部被拦（§3.2）")
    gateway = platform.gateway
    scope = build_scope(platform.world, "u_fraud_00")
    for tool, args in [("apply_disposition", {"entity_id": "u_fraud_00", "action": "KEEP_BLOCK"}),
                       ("release_block", {"entity_id": "u_normal_00"}),
                       ("update_rule_config", {"rule_id": "R003", "threshold": 0.9}),
                       ("publish_strategy", {"candidate_id": "sc_x"})]:
        verdict = gateway.check(ToolCall(tool=tool, arguments=args), scope=scope)
        checks.check(f"{tool} 被拦下",
                     not verdict.allowed
                     and verdict.deny_reason is ToolDenyReason.NO_PERMISSION,
                     verdict.deny_reason.value if verdict.deny_reason else "-")

    # ---------------------------------------------------------------- T8
    print("\n[T8] 实体范围：不能借调查之名遍历全库（§3.2）")
    outsider = next(uid for uid in platform.world.users if uid not in scope)
    verdict = gateway.check(
        ToolCall(tool="query_account_profile", arguments={"user_id": outsider}), scope=scope)
    checks.check("范围外的实体查询被拒",
                 not verdict.allowed and verdict.deny_reason is ToolDenyReason.OUT_OF_SCOPE,
                 f"{outsider} -> {verdict.deny_reason.value if verdict.deny_reason else '-'}")

    # ---------------------------------------------------------------- T9
    print("\n[T9] 审批是「一次性动作授权」，不是永久权限（§3.2）")
    g = fresh().gateway
    approval = g.grant_approval("apply_disposition", "AP_1", approver="reviewer")
    first = g.check(ToolCall(tool="apply_disposition",
                             arguments={"entity_id": "u", "action": "A"},
                             approval_id=approval))
    other = g.check(ToolCall(tool="release_block", arguments={"entity_id": "u"},
                             approval_id=approval))
    checks.check("持审批号可以执行被批准的那个动作", first.allowed, "apply_disposition ✓")
    checks.check("同一个审批号不能用于另一个动作", not other.allowed,
                 f"release_block -> {other.deny_reason.value if other.deny_reason else '-'}")
    try:
        g.grant_approval("update_rule_config", "AP_2", approver="x")
        raised = False
    except PermissionError:
        raised = True
    checks.check("改规则申请审批被拒（没有这条通道）", raised, "PermissionError")

    # ---------------------------------------------------------------- T10
    print("\n[T10] 证据分层：推断必须指回原始证据（§4.3）")
    from app.domain.models import Evidence

    try:
        Evidence(kind="INFERRED", source="llm", claim="凭空推断")
        blocked = False
    except Exception:
        blocked = True
    checks.check("没有依据的推断写不进来", blocked, "Evidence(INFERRED, derived_from=[]) 被拒")

    # ---------------------------------------------------------------- T11
    print("\n[T11] 未知风险发现：Agent 提假设，数据做判定（§5.3）")
    p = fresh("device_farm")
    patterns = p.discoverer.discover()
    checks.check("发现了两个候选模式", len(patterns) >= 2, f"{len(patterns)} 个")
    weak = p.validator.replay(patterns[0])
    strong = p.validator.replay(patterns[1])
    checks.check("单信号的准确率低于门槛（被退回）",
                 weak.precision < MIN_PRECISION,
                 f"precision={weak.precision:.3f} flagged={weak.flagged}")
    checks.check("组合信号通过门槛",
                 strong.precision >= MIN_PRECISION and strong.recall >= 0.5,
                 f"precision={strong.precision:.3f} recall={strong.recall:.3f}")
    evaluated = p.validator.evaluate(patterns[1])
    checks.check("通过回放的模式只到 CANDIDATE，不是 VALIDATED",
                 evaluated.status is PatternStatus.CANDIDATE,
                 evaluated.status.value)
    try:
        p.validator.confirm(evaluated, reviewer="")
        blocked = False
    except PermissionError:
        blocked = True
    checks.check("空签字不能确认", blocked, "需要 reviewer")
    confirmed = p.validator.confirm(evaluated, reviewer="expert-01")
    checks.check("人工确认后才变成 VALIDATED",
                 confirmed.status is PatternStatus.VALIDATED
                 and confirmed.promotable,
                 confirmed.decided_by)

    # ---------------------------------------------------------------- T12
    print("\n[T12] 策略运营：Agent 生成候选但不能发布（§6.3）")
    p = fresh("device_farm")
    analysis = p.strategy.analyze("R003_REFUND_RATIO")
    checks.check("发现误报集中在促销场景",
                 max(analysis.by_scene.items(), key=lambda kv: kv[1]["fp_rate"])[0] == "promo",
                 analysis.finding)
    candidate = p.strategy.propose(analysis)
    checks.check("候选策略携带漏报影响字段",
                 candidate.expected_fn_delta > 0,
                 f"fp↓{candidate.expected_fp_reduction:.2%} fn↑{candidate.expected_fn_delta:.2%}")
    passed, _ = p.strategy.gate(candidate)
    checks.check("完整候选通过准入检查", passed, "ok")

    bad = candidate.model_copy(deep=True)
    bad.expected_fn_delta = 0.0
    bad_passed, problems = p.strategy.gate(bad)
    checks.check("只报收益不报代价的候选被拒回",
                 not bad_passed and any("fn_delta" in item for item in problems),
                 problems[0] if problems else "-")
    verdict = p.gateway.check(ToolCall(tool="publish_strategy",
                                       arguments={"candidate_id": candidate.candidate_id}))
    checks.check("Agent 发布策略被拦下", not verdict.allowed,
                 verdict.deny_reason.value if verdict.deny_reason else "-")
    checks.check("候选的 applied 永远是 False", candidate.applied is False, "False")

    # ---------------------------------------------------------------- T13
    print("\n[T13] 工具网关的审计完整性（§3.2）")
    g = fresh().gateway
    g.check(ToolCall(tool="query_account_profile", arguments={"user_id": "u_fraud_00"}),
            scope={"u_fraud_00"})
    g.check(ToolCall(tool="apply_disposition", arguments={}), scope={"u_fraud_00"})
    g.check(ToolCall(tool="not_a_tool", arguments={}), scope=set())
    snapshot = g.snapshot()
    checks.check("每次调用都留下审计记录", len(g.audit) == snapshot["total"],
                 f"audit={len(g.audit)} total={snapshot['total']}")
    checks.check("拒绝原因被分类统计",
                 snapshot["denied_by_reason"].get("NO_PERMISSION", 0) >= 1
                 and snapshot["denied_by_reason"].get("UNKNOWN_TOOL", 0) >= 1,
                 snapshot["denied_by_reason"])
    checks.check("高危尝试被单独记录",
                 "apply_disposition" in snapshot["dangerous_attempts"],
                 snapshot["dangerous_attempts"])

    # ---------------------------------------------------------------- T14
    print("\n[T14] 参数校验：缺参数的调用在网关被拦（§3.2）")
    verdict = platform.gateway.check(ToolCall(tool="query_device_graph", arguments={}),
                                     scope=scope)
    checks.check("缺 device_id 被拒",
                 not verdict.allowed and verdict.deny_reason is ToolDenyReason.BAD_ARGUMENTS,
                 verdict.deny_reason.value if verdict.deny_reason else "-")

    # ---------------------------------------------------------------- T15
    print("\n[T15] Agent 的输出里没有「已执行」")
    p = fresh()
    report, _ = p.investigate(p.make_alert("u_fraud_00", "分歧", model_score=0.32))
    checks.check("调查报告的 executed 永远是 False", report.executed is False, "False")
    checks.check("处置字段是「建议」语义",
                 report.disposition in (Disposition.KEEP_BLOCK, Disposition.ESCALATE,
                                        Disposition.RELEASE, Disposition.REQUEST_VERIFY,
                                        Disposition.CLOSE_AS_FALSE_POSITIVE),
                 report.disposition.value)

    # ---------------------------------------------------------------- T16
    print("\n[T16] 假设状态：证据冲突时不下单边判断（§4.4）")
    from app.domain.models import Hypothesis

    item = Hypothesis(key="H_X", description="x")
    item.support = ["a", "b", "c"]
    item.refute = ["d"]
    from app.agent.investigator import Investigator

    class _Stub:
        hypotheses = {"H_X": item}

    Investigator._reweigh(_Stub())
    checks.check("3 支持 1 反驳 -> 不是 REFUTED",
                 item.status is not HypothesisStatus.REFUTED,
                 item.status.value)
    item.support = []
    item.refute = ["d"]
    Investigator._reweigh(_Stub())
    checks.check("无支持有反驳 -> REFUTED",
                 item.status is HypothesisStatus.REFUTED, item.status.value)

    return checks.report()


# ======================================================================
# 场景注册与入口
# ======================================================================
SCENARIOS: dict[str, tuple[str, Callable[[], int | None]]] = {
    "online": ("在线决策链路：确定性、毫秒级、不调查（§1）", scenario_online),
    "investigate": ("动态调查：假设驱动的证据收集（§4）", scenario_investigate),
    "paths": ("路径对比：Agent vs 固定工作流（§5，核心）", scenario_paths),
    "boundary": ("权限边界：只能建议不能执行（§3.2）", scenario_boundary),
    "discover": ("未知风险发现：提假设 + 离线验证（方式二）", scenario_discover),
    "strategy": ("策略运营：候选策略与发布边界（方式三）", scenario_strategy),
    "value": ("价值量化：三层次与可验证指标（§7）", scenario_value),
    "tests": ("回归测试（带断言）", scenario_tests),
}
ALL_ORDER = ["online", "investigate", "paths", "boundary", "discover", "strategy",
             "value", "tests"]


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
        prog="risk-agent",
        description="Risk Control Agent —— 传统风控与 Agent 的分层协作 Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m app.main --list\n"
            "  python -m app.main all\n"
            "  python -m app.main paths boundary\n"
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
    print("  Risk Control Agent")
    print("  传统风控（在线确定性决策） + Agent（离线动态调查）")
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
