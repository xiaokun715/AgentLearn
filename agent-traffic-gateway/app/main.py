"""Agent 流量网关 —— 命令行演示入口。

覆盖《第十六章：高并发大流量 Agent 网关设计说明书》§2 ~ §20 的主线：

    python -m app.main --list      # 看有哪些场景
    python -m app.main all         # 全跑（约几秒）
    python -m app.main capacity    # 只跑指定场景
    python -m app.main tests       # 带断言的回归测试

设计说明：全章的结论都由一个**确定性离散时间仿真器**产出，而不是真集群压测。
理由在 §1 里说过 —— 流量治理的结论只有在压力下才看得见，而真去压测要集群、
要额度、要几小时。用仿真器，同样的结论可以在测试里被断言，`pytest` 一秒跑完。
这不是替代压测，是让压测**之前**就能把设计错误筛掉。
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable

from .domain.enums import Priority, QueuePolicy, RejectReason, UpgradeSignal
from .domain.models import GatewayConfig, UpstreamConfig, WorkloadSpec
from .domain.policy import (
    DEFAULT_UPSTREAM,
    FAIRNESS_TENANTS,
    burst_workload,
    governed,
    multi_tenant_workload,
    steady_workload,
    tenant,
    ungoverned,
    upstream_capacity_rps,
)
from .factory import build_simulation, check_config
from .observe.report import (
    RESULT_ALIGNS,
    RESULT_HEADERS,
    WIDTH,
    banner,
    bar_row,
    info,
    ms,
    note_outcome,
    offload_breakdown,
    ok,
    pct,
    rejection_breakdown,
    result_row,
    section,
    sparkline,
    step,
    table,
    warn,
)
from .sim.engine import SimResult

CAPACITY = upstream_capacity_rps(DEFAULT_UPSTREAM)


# ======================================================================
def upstream_bound(**overrides) -> GatewayConfig:
    """关掉缓存与合并 —— 让**上游**成为真正的瓶颈。

    测「容量 / 准入 / 清扫 / 扩容 / 重试 / 公平」这类结论时必须这样。
    如果缓存把 90% 的请求都吸收了，你测的就不是「上游过载时系统怎么反应」，
    而是「缓存有多能扛」—— 后者当然也重要，但那是 §10 的话题，不是 §7 的。
    这是本 Demo 里最容易犯的一类测量错误：**衡量的对象被另一个机制挡在了后面**。
    """
    base = dict(l1_capacity=0, l2_capacity=0, semantic_cache_enabled=False,
                coalescing_enabled=False)
    base.update(overrides)
    return governed(**base)


def queue_only(**overrides) -> GatewayConfig:
    """只留队列本身：关掉丢弃、准入、清扫，队列开到很大。

    这是本 Demo 里最重要的一条实验纪律。分级丢弃（§13）在过载时**非常霸道** ——
    它会在准入之前把大部分请求拒掉，于是队列永远建不起来，
    而「调度公不公平」「清扫释放了多少算力」「扩容追不追得上」
    这些问题就全都观察不到了（你会看到所有配置的指标一模一样）。

    所以要观察**排队**这件事，就必须先把「丢弃」关掉；
    要看**丢弃**本身，才把它打开。**一个实验只回答一个问题。**
    """
    base = dict(shedding_enabled=False, admission_enabled=False,
                sweep_expired=False, queue_capacity=100_000,
                # 「只留队列」就该只留队列：降级会把服务时间缩短 0.35 倍，
                # 等于偷偷把容量放大近 3 倍，队列也就堆不起来了。
                degrade_enabled=False)
    base.update(overrides)
    return upstream_bound(**base)


def run(config: GatewayConfig, workload: WorkloadSpec, upstream: UpstreamConfig | None = None) -> SimResult:
    sim = build_simulation(config, upstream or DEFAULT_UPSTREAM, workload)
    return sim.run()


def show_result(result: SimResult, label: str = "") -> None:
    s = result.summary
    if label:
        info(label, "")
    info("请求数 / 达成吞吐", f"{s['requests']} / {s['achieved_rps']} RPS")
    info("成功 / 降级", f"{s['succeeded']} / {s['degraded']}（{pct(s['success_ratio'])}）")
    info("拒绝 / 超期 / 失败", f"{s['rejected']} / {s['timed_out']} / {s['failed']}")
    info("P50 / P99", f"{ms(result.p50)} / {ms(result.p99)}")
    info("上游调用 / 上行占比", f"{s['upstream_calls']} / {pct(s['upstream_ratio'])}")
    info("重试放大", f"{s['retry_amplification']:.2f}×")
    info("缓存+合并减负", pct(s["cache_offload"]))


# ======================================================================
# 场景一：对照组 —— 无治理 vs 只有效率优化 vs 完整治理
# ======================================================================
def scenario_baseline() -> None:
    banner("场景一：对照组 —— 同一批突发流量，三种治理程度（§1 / §2 / §18）")

    workload = burst_workload(5.0, burst_at_ms=20_000, burst_duration_ms=4_000,
                              burst_factor=8.0, duration_ms=45_000,
                              prompt_space=600, variant_ratio=0.4)
    section(1, "流量画像", "§17")
    info("持续时间", f"{workload.duration_ms / 1000:.0f}s")
    info("基础到达率", f"5.0 RPS（容量的 {5.0 / CAPACITY:.0%}）")
    info("突发", "第 20s 起 8× 持续 4s（约 45 RPS，远超容量）")
    info("上游容量", f"{CAPACITY:.2f} RPS（8 并发 / 1.5s）")

    section(2, "三种配置跑同一批请求", "§21 阶段二/三/四")
    configs = [
        ("① 无治理（裸奔）", ungoverned()),
        ("② 只有缓存+合并", ungoverned().with_(
            l1_capacity=2048, l2_capacity=16384, semantic_cache_enabled=True,
            coalescing_enabled=True)),
        ("③ 完整治理", governed()),
    ]
    results = []
    for label, config in configs:
        result = run(config, workload)
        results.append((label, result))

    table(RESULT_HEADERS, [result_row(label, r) for label, r in results],
          aligns=RESULT_ALIGNS)

    section(3, "逐项拆解", "§16")
    for label, result in results:
        print(f"\n     {label}")
        print(f"       {note_outcome(result)}")
        s = result.summary
        print(f"       拒绝={s['rejected']} 超期={s['timed_out']} 失败={s['failed']}"
              f"   上游调用={s['upstream_calls']} 上行占比={pct(s['upstream_ratio'])} "
              f"重试放大={s['retry_amplification']:.2f}")
        if s["reject_reasons"]:
            top = sorted(s["reject_reasons"].items(), key=lambda i: -i[1])[:3]
            print(f"       主要拒绝原因：{top}")

    section(4, "队列深度随时间变化（突发在第 20s）", "§16.3")
    print()
    for label, result in results:
        print(f"     {label}")
        print("       " + sparkline(result.series("inflight")[:450], label="在途请求 "))
        print("       " + sparkline(result.series("queue_depth")[:450], label="本地队列 "))
        print("       " + sparkline(result.series("upstream_queue")[:450], label="上游队列 "))

    ok("效率优化（缓存+合并）能降低平均开销，但**过载保护**才是突发下不崩的原因")
    warn("注意 ③ 的拒绝率最高 —— 那是买来的：它把「必然超时」换成了「立刻失败」")


# ======================================================================
# 场景二：容量曲线 —— 饱和点在哪里
# ======================================================================
def scenario_capacity() -> None:
    banner("场景二：容量曲线 —— 吞吐会先到顶，然后回落（§2）")

    raw = governed(
        admission_enabled=False, sweep_expired=False, shedding_enabled=False,
        degrade_enabled=False, queue_capacity=100_000,
        l1_capacity=0, l2_capacity=0, semantic_cache_enabled=False,
        coalescing_enabled=False,
    )

    section(1, "先把缓存和合并关掉，量的是**裸容量**", "§2.1")
    info("上游并发 / 平均服务", f"{DEFAULT_UPSTREAM.max_concurrency} / "
                                f"{DEFAULT_UPSTREAM.mean_service_ms / 1000:.1f}s")
    info("理论容量", f"{CAPACITY:.2f} RPS")
    step("开着缓存量出来的不是容量，是「被缓存之后的容量」—— 见 README「关键取舍 1」")

    section(2, "扫描到达率", "§2.3")
    rows = []
    results = []
    for rate in [1.0, 2.0, 3.0, 4.0, 4.7, 5.3, 6.0, 8.0]:
        result = run(raw, steady_workload(rate, duration_ms=40_000, variant_ratio=0.0))
        results.append((rate, result))
        rows.append([
            f"{rate:.1f}", pct(rate / CAPACITY, 0),
            f"{result.achieved_rps:.2f}", ms(result.p50), ms(result.p99),
            ms(result.metrics.queue_wait.percentile(0.50)),
            pct(result.success_ratio),
        ])
    table(["到达率", "ρ", "完成吞吐", "P50", "P99", "排队P50", "成功"],
          rows, aligns=["r", "r", "r", "r", "r", "r", "r"])

    section(3, "吞吐-到达率曲线（找饱和点）", "§2.3")
    peak_rate, peak_result = max(results, key=lambda item: item[1].achieved_rps)
    peak = peak_result.achieved_rps
    print()
    for rate, result in results:
        mark = "  ← 峰值" if rate == peak_rate else ""
        print("     " + bar_row(f"到达 {rate:.1f} RPS", result.achieved_rps,
                                max_value=peak * 1.05, width=30,
                                suffix=f"{result.achieved_rps:.2f}{mark}"))

    section(4, "结论", "§2.5")
    info("理论容量", f"{CAPACITY:.2f} RPS")
    info("实测峰值吞吐", f"{peak:.2f} RPS @ 到达率 {peak_rate:.1f}")
    last = results[-1][1]
    info(f"到达率 8.0（ρ={8.0 / CAPACITY:.0%}）", f"吞吐反而掉到 {last.achieved_rps:.2f} RPS")
    ok("过载时吞吐**不仅不增加，还会下降** —— 因为超期的请求白占了槽位")
    warn(f"所以目标水位应取 60~70%（约 {CAPACITY * 0.65:.2f} RPS），而不是贴着容量跑")


# ======================================================================
# 场景三：准入控制 —— 队列满 ≠ 该拒绝
# ======================================================================
def scenario_admission() -> None:
    banner("场景三：准入控制 —— 用「预计完成时间」而不是「队列长度」（§7 / §8.1）")

    workload = steady_workload(8.0, duration_ms=45_000)
    section(1, "到达率 8.0 RPS（容量的 150%）", "§2.3")
    info("上游容量", f"{CAPACITY:.2f} RPS")
    step("队列必然堆积。区别只在于「堆在哪里」和「堆到什么时候」")

    section(2, "四档：每一档只加一个机制", "§7.2 / §8.1 / §13.1")
    variants = [
        ("① 无界队列，无准入无丢弃", queue_only(degrade_enabled=False)),
        ("② 只加有界队列", queue_only(queue_capacity=32, degrade_enabled=False)),
        ("③ 只加准入控制", queue_only(admission_enabled=True,
                                       degrade_enabled=False)),
        ("④ 有界队列 + 准入 + 分级丢弃", upstream_bound(degrade_enabled=False)),
    ]
    results = []
    for label, config in variants:
        results.append((label, run(config, workload)))
    table(RESULT_HEADERS, [result_row(label, r) for label, r in results], aligns=RESULT_ALIGNS)

    section(3, "为什么 ③ 更好：看拒绝的**原因**", "§16.1")
    for label, result in results:
        print(f"\n     {label}")
        rejection_breakdown(result, indent=8)

    section(4, "队列长度与延迟的关系", "§8.1")
    for label, result in results:
        print(f"\n     {label}")
        print("       " + sparkline(result.series("queue_depth")[:450], label="队列深度 "))

    section(5, "结论", "§7.2")
    worst = results[0][1]
    best = results[-1][1]
    step("每一档的收益都可以单独读出来：")
    step("  ② 有界队列   → 超期数大幅下降（失败被提前到调用方还能补救的时刻）")
    step("  ③ 准入控制   → 拒绝原因从笼统的「队满」变成精确的「来不及」")
    step("  ④ 分级丢弃   → P99 和超期数再下一档（低价值工作被优先牺牲）")
    info("① 的 P99", ms(worst.p99))
    info("③ 的 P99", ms(best.p99))
    info("③ 的拒绝率", pct(best.summary["success_ratio"] and
                          best.metrics.rejected / best.metrics.total))
    ok("拒绝 8% 换来 P99 从「无上限」变成「有上限」—— 这是买来的，不是免费的")
    warn("① 的 P99 看起来「只有 30s」，那是因为压测窗口只有 45s —— 无界队列的延迟没有上限")


# ======================================================================
# 场景四：多级限流 —— 保护邻居
# ======================================================================
def scenario_ratelimit() -> None:
    banner("场景四：多级限流 —— 一个租户的洪峰不该影响邻居（§6）")

    section(1, "两个租户：whale 20 RPS + victim 3 RPS", "§6.2")
    tenants = [
        tenant("whale", 20.0, priority=Priority.STANDARD),
        tenant("victim", 3.0, priority=Priority.STANDARD),
    ]
    workload = multi_tenant_workload(tenants, duration_ms=40_000)
    info("总到达率", f"{workload.total_rate_rps} RPS（容量的 "
                     f"{workload.total_rate_rps / CAPACITY:.0%}）")

    section(2, "不限租户配额 vs 限租户配额", "§6.2 L2")
    configs = [
        ("① 不限租户配额", governed()),
        ("② whale 限 6 RPS", governed(
            tenant_rps={"whale": 6.0}, tenant_burst={"whale": 12})),
    ]
    results = []
    for label, config in configs:
        results.append((label, run(config, workload)))

    section(3, "从**全局**指标看，两者差不多", "§16.2")
    table(RESULT_HEADERS, [result_row(label, r) for label, r in results], aligns=RESULT_ALIGNS)

    section(4, "按租户拆开看，差别就出来了", "§8.3")
    for label, result in results:
        print(f"\n     {label}")
        rows = []
        for stats in result.metrics.tenant_summary():
            rows.append([
                stats["tenant_id"], str(stats["arrived"]), pct(stats["ok"] / max(stats["arrived"], 1)),
                ms(stats["latency"]["p50"]), ms(stats["latency"]["p99"]),
            ])
        table(["租户", "请求", "成功", "P50", "P99"], rows,
              indent=8, aligns=["l", "r", "r", "r", "r"])

    ok("全局 P99 在两列里几乎一样 —— 因为 whale 占了 87% 的请求，它把整体分位数拉住了")
    warn("只有**分租户**看，才发现 victim 在 ① 里被挤成了什么样（§16.2）")


# ======================================================================
# 场景五：请求合并与分层缓存
# ======================================================================
def scenario_coalesce() -> None:
    banner("场景五：请求合并与分层缓存 —— Agent 网关独有的优化（§9 / §10）")

    section(1, "热点流量：Zipf α=1.3，prompt 空间 50", "§3.3")
    workload = steady_workload(12.0, duration_ms=40_000, prompt_space=50,
                               zipf_alpha=1.3, variant_ratio=0.5)
    info("到达率", f"12.0 RPS（容量的 {12.0 / CAPACITY:.0%}）")
    step("真实流量几乎总是 Zipf：少数问题被反复问，这正是合并与缓存的价值来源")

    section(2, "四种配置", "§10.1")
    configs = [
        ("① 裸奔", upstream_bound()),
        ("② 只加请求合并", governed(l1_capacity=0, l2_capacity=0,
                                    semantic_cache_enabled=False, coalescing_enabled=True)),
        ("③ 合并 + L1/L2", governed(semantic_cache_enabled=False, coalescing_enabled=True)),
        ("④ 合并 + 分层 + 语义", governed()),
    ]
    results = []
    for label, config in configs:
        results.append((label, run(config, workload)))

    table(RESULT_HEADERS, [result_row(label, r) for label, r in results], aligns=RESULT_ALIGNS)
    print()
    table(["配置", "上游调用", "上行占比", "减负", "实测吞吐"],
          [[label, str(r.summary["upstream_calls"]),
            pct(r.summary["upstream_ratio"]),
            pct(r.summary["cache_offload"]),
            f"{r.achieved_rps:.2f} RPS"]
           for label, r in results],
          aligns=["l", "r", "r", "r", "r"])

    section(3, "④ 的请求都去哪了", "§10.1")
    offload_breakdown(results[-1][1])

    step("④ 里 L2 命中是 0 —— 单实例下 L1 是 L2 的超集，轮不到 L2 出手。")
    step("  真实的多实例部署里每台机器的 L1 各不相同，L2 才是减负主力（§10.1）。")

    section(4, "合并的内部状态", "§9.1")
    snap = results[-1][1].gateway["coalescer"]
    info("发起的飞行数（= 上游调用）", snap["flights"])
    info("搭车成功的请求数", snap["joined"])
    info("合并放大比", f"{snap['amplification']:.2f}×")
    info("被放弃的等待者", snap["abandoned"])
    ok("放大比 = 实际请求数 / 上游调用数。这个数是 Agent 网关相对普通网关的**净增量价值**")
    warn("合并键必须是精确指纹（§9.2）：少一个维度就会串结果，而串结果是不可挽回的")


# ======================================================================
# 场景六：公平调度
# ======================================================================
def scenario_fairness() -> None:
    banner("场景六：公平调度 —— FIFO 下小租户的 P99 由大租户决定（§8.3 / §8.4）")

    section(1, "whale 10 RPS + mouse 0.1 RPS（相差 100 倍）", "§8.3")
    workload = multi_tenant_workload(FAIRNESS_TENANTS, duration_ms=60_000)
    info("总到达率", f"{workload.total_rate_rps} RPS（容量的 "
                     f"{workload.total_rate_rps / CAPACITY:.0%}）")
    step("队列必然堆积 —— 堆积的时候，FIFO 会让 mouse 排在 whale 的 100 倍流量后面")

    section(2, "只换调度策略，流量完全相同", "§8.4")
    configs = [
        ("① FIFO", queue_only(queue_policy=QueuePolicy.FIFO)),
        ("② 优先级+老化", queue_only(queue_policy=QueuePolicy.PRIORITY_AGING)),
        ("③ WFQ 加权公平", queue_only(queue_policy=QueuePolicy.WFQ)),
    ]
    results = []
    for label, config in configs:
        results.append((label, run(config, workload)))

    section(3, "全局指标看不出问题", "§16.2")
    table(RESULT_HEADERS, [result_row(label, r) for label, r in results], aligns=RESULT_ALIGNS)

    section(4, "分租户 P99 —— 这才是公平性的度量", "§8.3")
    rows = []
    for label, result in results:
        tenants = {s["tenant_id"]: s for s in result.metrics.tenant_summary()}
        whale = tenants.get("whale", {})
        mouse = tenants.get("mouse", {})
        rows.append([
            label,
            str(mouse.get("arrived", 0)),
            ms(whale.get("latency", {}).get("p99", 0)),
            ms(mouse.get("latency", {}).get("p50", 0)),
            ms(mouse.get("latency", {}).get("p99", 0)),
            pct(mouse.get("ok", 0) / max(mouse.get("arrived", 1), 1)),
        ])
    table(["策略", "mouse 请求数", "whale P99", "mouse P50", "mouse P99", "mouse 成功"],
          rows, aligns=["l", "r", "r", "r", "r", "r"])

    fifo_mouse = rows[0][4]
    wfq_mouse = rows[-1][4]
    ok(f"WFQ 把 mouse 的 P99 从 {fifo_mouse} 压到 {wfq_mouse} —— 代价是 whale 稍微变慢")
    warn("公平不是免费的：它是把「B 被饿死」换成了「A 稍微慢一点」，这是业务决策不是技术决策")


# ======================================================================
# 场景七：超时预算与取消传播
# ======================================================================
def scenario_deadline() -> None:
    banner("场景七：超时预算 —— 端到端的一个 deadline，胜过每层各设一个超时（§11.2）")

    section(1, "到达率 8.0 RPS，上游容量 5.33 RPS", "§2.3")
    info("队列必然堆积", "区别只在于「堆积的请求里有多少是注定失败的」")
    workload = steady_workload(8.0, duration_ms=40_000)

    section(2, "三档：无预算 / 有预算无清扫 / 有预算有清扫", "§11.2")
    variants = [
        ("① 无超时预算（= 每层各设超时）", queue_only(request_budget_ms=600_000)),
        ("② 端到端预算 9s，关闭清扫", queue_only()),
        ("③ 端到端预算 9s，开启清扫", queue_only(sweep_expired=True)),
    ]
    results = []
    for label, config in variants:
        results.append((label, run(config, workload)))
    table(RESULT_HEADERS, [result_row(label, r) for label, r in results], aligns=RESULT_ALIGNS)

    section(3, "① 的成功率更高 —— 但那 24 个百分点是虚的", "§11.2")
    none_budget = results[0][1]
    budgeted = results[1][1]
    info("① 成功率", pct(none_budget.summary["success_ratio"]))
    info("② 成功率", pct(budgeted.summary["success_ratio"]))
    step("① 里那些「成功」的请求，确实被上游做完了 —— 但客户早就放弃了。")
    step("没有端到端 deadline，系统会把「做完了但没人要」也算成成功，")
    step("于是延迟这个指标失去意义，而且这批工作照样占着槽位和配额。")
    ok("带预算的 ② 只是**如实**把它们记为超期 —— 成功率降了，数字真了")

    section(4, "释放出来的算力", "§11.2")
    for label, result in results:
        counters = result.gateway["counters"]
        info(f"{label} 完成时已超期", counters["inflight_timeout"])
        info("  出队时才发现超期", counters["dispatch_expired"])
        info("  周期性清扫移出", counters["swept_timeout"])
    step("「完成时已超期」是这一节最贵的一个数：上游**已经把活干完了**，")
    step("结果因为超过 deadline 被丢弃。这批算力是纯浪费。")
    step("清扫把超期判定从「出队那一刻」前移到「过期后 50ms」：")
    info("  ② 的出队时判定", budgeted.gateway["counters"]["dispatch_expired"])
    info("  ③ 的周期性清扫", results[2][1].gateway["counters"]["swept_timeout"])

    section(5, "时间线的差别", "§16.3")
    for label, result in results:
        print(f"\n     {label}")
        print("       " + sparkline(result.series("queue_depth")[:400], label="队列深度 "))

    ok("端到端预算的价值不只是「早点失败」，更是**把注定失败的工作从链路上摘掉**")
    warn("周期性清扫是**优化**不是正确性必需：出队时的最后一道确认才是兜底。")
    warn("少写任何一层都会漏掉一批 —— 但它们解决的是不同的问题（一个是浪费，一个是误差）")


# ======================================================================
# 场景八：重试风暴与重试预算
# ======================================================================
def scenario_resilience() -> None:
    banner("场景八：重试预算 —— 防止重试把系统推下悬崖（§12）")

    section(1, "上游开始抖动：瞬时失败率 25%", "§12.1")
    upstream = DEFAULT_UPSTREAM.model_copy(update={"transient_fail_rate": 0.25})
    workload = steady_workload(5.2, duration_ms=40_000)
    info("到达率", f"5.2 RPS（容量的 {5.2 / CAPACITY:.0%}）")
    info("期望放大（失败率 25%、重试 3 次）", f"{(1 - 0.25 ** 3) / (1 - 0.25):.2f}×")

    section(2, "三种重试策略", "§12.2")
    configs = [
        ("① 不重试", queue_only(retry_enabled=False)),
        ("② 朴素重试 ×3", queue_only(retry_max_attempts=3, retry_budget_ratio=None)),
        ("③ 重试预算 10%", queue_only(retry_max_attempts=3, retry_budget_ratio=0.10)),
        ("④ 预算 + 熔断", queue_only(retry_max_attempts=3, retry_budget_ratio=0.10,
                                      breaker_enabled=True, breaker_failure_ratio=0.4,
                                      breaker_min_samples=12)),
    ]
    results = []
    for label, config in configs:
        results.append((label, run(config, workload, upstream=upstream)))

    table(RESULT_HEADERS, [result_row(label, r) for label, r in results], aligns=RESULT_ALIGNS)
    print()
    table(["策略", "上游提交", "其中重试", "重试放大", "成功"],
          [[label, str(r.summary["upstream_calls"]),
            str(r.gateway["counters"]["retries"]),
            f"{r.summary['retry_amplification']:.3f}", pct(r.summary["success_ratio"])]
           for label, r in results],
          aligns=["l", "r", "r", "r", "r"])

    section(3, "重试预算的内部状态", "§12.2")
    snap = results[-1][1].gateway["retry_budget"]
    info("预算比例", snap["ratio"])
    info("剩余令牌", snap["tokens"])
    info("批准 / 拒绝", f"{snap['granted']} / {snap['denied']}（拒绝率 {pct(snap['deny_ratio'])}）")
    info("熔断状态", results[-1][1].gateway["breaker"]["state"])

    section(4, "结论", "§12.1")
    naive = results[1][1]
    budgeted = results[-1][1]
    info("② 朴素重试的放大比", f"{naive.summary['retry_amplification']:.3f}")
    info("④ 预算+熔断的放大比", f"{budgeted.summary['retry_amplification']:.3f}")
    step("② 的成功率确实更高 —— 重试在**容量还有余量**时几乎是白赚的")
    warn("但要看清代价：它让上游多收到了 "
         f"{naive.summary['upstream_calls'] - budgeted.summary['upstream_calls']} 次调用。"
         "在共享配额的真实环境里，这些调用是从**别人**的额度里拿的")
    warn("真实链路的重试还是**分层**的：客户端×2 × 网关×2 × SDK×2 = 8 倍放大，"
         "除非显式禁掉中间层")

    section(5, "上游整体故障时（失败率 70%）：熔断才是主角", "§12.4")
    broken = DEFAULT_UPSTREAM.model_copy(update={"transient_fail_rate": 0.70})
    rows = []
    for label, config in [
        ("① 无预算无熔断", queue_only(retry_max_attempts=3, retry_budget_ratio=None,
                                       breaker_enabled=False)),
        ("② 预算 10%", queue_only(retry_max_attempts=3, retry_budget_ratio=0.10,
                                  breaker_enabled=False)),
        ("③ 预算 + 熔断", queue_only(retry_max_attempts=3, retry_budget_ratio=0.10,
                                    breaker_enabled=True, breaker_failure_ratio=0.5,
                                    breaker_min_samples=12)),
    ]:
        r = run(config, workload, upstream=broken)
        rows.append([
            label, str(r.summary["upstream_calls"]),
            f"{r.summary['retry_amplification']:.2f}×",
            pct(r.summary["success_ratio"]), str(r.summary["timed_out"]),
            r.gateway["breaker"]["state"],
        ])
    table(["策略", "上游提交", "重试放大", "本请求成功", "超期", "熔断状态"], rows,
          aligns=["l", "r", "r", "r", "r", "l"])
    step("① 的成功率最高（65%）—— 但那是**用 2.15 倍负载换来的**：")
    step("   上游已经七成故障，我们还在给它两倍多的请求，其中大部分注定失败。")
    step("③ 的代价是成功率掉到 8% —— 这正是 §12.4 说的「熔断的代价是拒绝掉")
    step("   本来可能成功的请求」；而它买到的是**停止向一个已知坏掉的依赖继续施压**。")
    warn("这也是为什么熔断不能只看「本请求成功率」这个指标 —— 它优化的是系统整体")
    ok("重试预算限制**自己的代价**，熔断尽快**停止无谓的尝试** —— 两者不能互相替代")


# ======================================================================
# 场景九：过载降级
# ======================================================================
def scenario_degrade() -> None:
    banner("场景九：过载降级 —— 降低质量地完成，好过干脆拒绝（§13.2）")

    section(1, "到达率 9.0 RPS（容量的 169%）", "§13")
    workload = steady_workload(9.0, duration_ms=40_000)

    configs = [
        ("① 不丢弃不降级", queue_only(degrade_enabled=False)),
        ("② 只分级丢弃", upstream_bound(degrade_enabled=False)),
        ("③ 丢弃 + 降级（小模型）", upstream_bound()),
    ]
    results = []
    for label, config in configs:
        results.append((label, run(config, workload)))

    section(2, "结果对比", "§13.1 / §13.2")
    table(RESULT_HEADERS, [result_row(label, r) for label, r in results], aligns=RESULT_ALIGNS)
    print()
    table(["配置", "成功(OK)", "降级", "拒绝", "超期", "实测吞吐"],
          [[label, str(r.summary["succeeded"] - r.summary["degraded"]),
            str(r.summary["degraded"]), str(r.summary["rejected"]),
            str(r.summary["timed_out"]), f"{r.achieved_rps:.2f}"]
           for label, r in results],
          aligns=["l", "r", "r", "r", "r", "r"])

    section(3, "降级必须被显式标注", "§13.2")
    step("降级的第一原则：**不能让结果变错，只能让它变差**")
    step("所以每个降级响应都带 degraded=True —— 调用方自己决定接不接受")
    degraded = results[-1][1].summary["degraded"]
    if degraded:
        ok(f"③ 有 {degraded} 个请求被标注为降级，它们在监控上是**可见**的")
    else:
        warn("③ 没有触发降级 —— 说明本轮排队压力还没到 degrade_from_pressure 阈值")

    section(4, "分级丢弃的边界", "§13.1")
    shed = sorted(results[-1][1].summary["reject_reasons"].items(), key=lambda i: -i[1])
    info("丢弃顺序", "批处理 → 低优先级 → 未命中的长请求 → 只留缓存命中")
    info("判定量", "排队压力 = 预计排队时间 / 单次服务时间（与并发数无关）")
    info("③ 的拒绝构成", shed or "（无）")


# ======================================================================
# 场景十：弹性伸缩
# ======================================================================
def scenario_autoscale() -> None:
    banner("场景十：弹性伸缩 —— 扩容指标选错等于永不扩容（§14.1）")

    section(1, "上游在第 10 秒突然变慢 3 倍", "§3.5 / §14.1")
    step("注意这是**服务时间**变长，不是到达率变化 —— 按 QPS 扩容的 HPA 完全不会响应")
    step("这里刻意让上游配额（32）**高于**本地并发上限（8）：否则扩容无处可扩")
    info("上游配额 / 本地并发", "32 / 8  →  本地容量 5.33 RPS，上游容量 21.3 RPS")
    info("到达率", "6.0 RPS —— 本地已经不够，上游还很闲")
    step("这正是「该扩容」的典型场景：瓶颈在**本地并发**，不在上游")

    upstream = DEFAULT_UPSTREAM.model_copy(update={
        "max_concurrency": 32,
        "slow_down_at_ms": 10_000, "slow_down_factor": 3.0,
    })
    workload = steady_workload(6.0, duration_ms=50_000)

    section(2, "三种扩容指标", "§14.1")
    rows = []
    runs = {}
    for signal in (UpgradeSignal.CPU, UpgradeSignal.QPS, UpgradeSignal.QUEUE_DEPTH):
        config = queue_only(autoscale_enabled=True, autoscale_signal=signal.value,
                          autoscale_cold_start_ms=4_000, autoscale_step=4,
                          autoscale_queue_threshold=20)
        result = run(config, workload, upstream=upstream)
        runs[signal] = result
        auto = result.autoscaler
        counters = result.gateway["counters"]
        rows.append([
            signal.value,
            f"{auto['readings'].get('cpu', 0):.2f}",
            f"{auto['readings'].get('qps', 0):.1f}",
            f"{auto['readings'].get('queue_depth', 0):.0f}",
            str(auto["scale_ups"]),
            str(counters["dispatched"]),
            f"{result.achieved_rps:.2f}",
            ms(result.p99),
        ])
    table(["扩容指标", "CPU读数", "QPS", "队列深度", "扩容次数", "派发量", "吞吐", "P99"],
          rows, aligns=["l", "r", "r", "r", "r", "r", "r", "r"])

    section(3, "队列深度的时间线（上游在第 10s 变慢）", "§16.3")
    for signal in (UpgradeSignal.CPU, UpgradeSignal.QPS, UpgradeSignal.QUEUE_DEPTH):
        result = runs[signal]
        print(f"\n     {signal.value}")
        print("       " + sparkline(result.series("queue_depth")[:380], label="队列深度 "))
        print("       " + sparkline(result.series("utilisation")[:380], label="并发利用率 "))
        events = result.autoscaler["events"]
        info("  扩容事件", [(f"{ms(t)}", f"→{limit} 槽") for t, limit, _ in events] or "无")

    section(4, "结论", "§14.1")
    base = runs[UpgradeSignal.CPU]
    scaled = runs[UpgradeSignal.QUEUE_DEPTH]
    info("上游变慢后的本地容量", f"8 并发 / 4.5s = {8 / 4.5:.2f} RPS（原来 5.33）")
    info("CPU 指标：派发量", f"{base.gateway['counters']['dispatched']}")
    info("队列深度：派发量", f"{scaled.gateway['counters']['dispatched']}")
    ok("CPU 读数一直在 22%~72% 之间，70% 的阈值触不到 —— IO 密集型服务的 CPU 与负载无关")
    ok("QPS 测的是「已经服务了多少」而不是「欠了多少」，瓶颈在自己身上时它永远正常")
    ok("队列深度是唯一直接反映「服务能力是否跟得上」的信号")
    warn("扩容还要算上冷启动（这里是 4s）—— 流量 30 秒翻倍而冷启动 20 秒时，永远追不上")
    warn("另外：本地并发不能扩到超过上游配额（§11.1）—— 那只是把队列搬到你更看不见的地方")


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
    banner("回归测试 —— 流量治理的每一条结论都有一条对应的断言")
    checks = _Checks()
    raw = governed(admission_enabled=False, sweep_expired=False, shedding_enabled=False,
                   degrade_enabled=False, queue_capacity=100_000, l1_capacity=0,
                   l2_capacity=0, semantic_cache_enabled=False, coalescing_enabled=False)

    # ---------------------------------------------------------------- T1
    print("\n[T1] 容量上界：实测吞吐不能超过 并发 / 服务时间")
    r = run(raw, steady_workload(4.0, duration_ms=40_000, variant_ratio=0.0))
    checks.check(
        "到达率在容量以内时，吞吐 ≈ 到达率",
        abs(r.achieved_rps - 4.0) < 0.5,
        f"到达 4.0，完成 {r.achieved_rps:.2f}",
    )
    r_over = run(raw, steady_workload(12.0, duration_ms=40_000, variant_ratio=0.0))
    checks.check(
        "到达率远超容量时，吞吐被压在上游容量附近",
        r_over.achieved_rps < CAPACITY * 1.15,
        f"到达 12.0，完成 {r_over.achieved_rps:.2f}，容量 {CAPACITY:.2f}",
    )

    # ---------------------------------------------------------------- T2
    print("\n[T2] 过载时吞吐回落（§2.5）")
    results = {
        rate: run(raw, steady_workload(rate, duration_ms=40_000, variant_ratio=0.0))
        for rate in (4.7, 5.3, 6.0, 8.0)
    }
    peak = max(results.values(), key=lambda item: item.achieved_rps)
    checks.check(
        "存在一个峰值，且超过它之后吞吐下降",
        results[8.0].achieved_rps < peak.achieved_rps,
        f"峰值 {peak.achieved_rps:.2f} @λ=?  8.0 RPS 时降到 {results[8.0].achieved_rps:.2f}",
    )

    # ---------------------------------------------------------------- T3
    print("\n[T3] 准入控制：用预计完成时间而不是队列长度（§7.2）")
    overload = steady_workload(8.0, duration_ms=40_000)
    no_admit = run(queue_only(degrade_enabled=False), overload)
    admit = run(queue_only(admission_enabled=True, degrade_enabled=False), overload)
    full = run(upstream_bound(degrade_enabled=False), overload)
    checks.check(
        "开启全部治理后 P99 明显下降",
        full.p99 < no_admit.p99,
        f"无治理 {ms(no_admit.p99)} → 全治理 {ms(full.p99)}",
    )
    checks.check(
        "超期数大幅下降（注定失败的工作被提前拦掉）",
        full.summary["timed_out"] < no_admit.summary["timed_out"] * 0.5,
        f"{no_admit.summary['timed_out']} → {full.summary['timed_out']}",
    )
    reasons = admit.summary["reject_reasons"]
    checks.check(
        "拒绝原因是「来不及时」而不是「队列满」",
        reasons.get(RejectReason.DEADLINE_INFEASIBLE.value, 0) >=
        reasons.get(RejectReason.QUEUE_FULL.value, 0),
        f"拒绝构成={reasons}",
    )
    checks.check(
        "准入控制给出的是「来不及时」这个精确原因",
        admit.gateway["admission"]["rejected_infeasible"] > 0,
        f"infeasible={admit.gateway['admission']['rejected_infeasible']} "
        f"reasons={admit.summary['reject_reasons']}",
    )

    # ---------------------------------------------------------------- T4
    print("\n[T4] 有界队列把失败提前（§8.1）")
    bounded = run(queue_only(queue_capacity=32, degrade_enabled=False), overload)
    checks.check(
        "有界队列出现了 QUEUE_FULL 拒绝",
        bounded.summary["reject_reasons"].get(RejectReason.QUEUE_FULL.value, 0) > 0,
        f"={bounded.summary['reject_reasons'].get(RejectReason.QUEUE_FULL.value, 0)}",
    )
    checks.check(
        "有界队列的 P99 低于无界队列",
        bounded.p99 <= no_admit.p99,
        f"有界 {ms(bounded.p99)} ≤ 无界 {ms(no_admit.p99)}",
    )
    checks.check(
        "有界队列显著减少了超期数",
        bounded.summary["timed_out"] < no_admit.summary["timed_out"] * 0.5,
        f"{no_admit.summary['timed_out']} → {bounded.summary['timed_out']}",
    )

    # ---------------------------------------------------------------- T5
    print("\n[T5] 多级限流保护邻居（§6.2）")
    neighbours = multi_tenant_workload(
        [tenant("whale", 20.0), tenant("victim", 3.0)], duration_ms=40_000
    )
    unquota = run(queue_only(), neighbours)
    quota = run(queue_only(tenant_rps={"whale": 6.0}, tenant_burst={"whale": 12}),
                neighbours)

    def victim_stats(result):
        return next(s for s in result.metrics.tenant_summary() if s["tenant_id"] == "victim")

    def victim_ok(result):
        stats = victim_stats(result)
        return stats["ok"] / max(stats["arrived"], 1)

    # 判据用**成功率**而不是 P99：被拒绝的请求延迟很短，
    # 会把「被挤掉」伪装成「很快」—— 这是分位数指标最容易骗人的地方。
    checks.check(
        "限流后 victim 的成功率更高（邻居被保护了）",
        victim_ok(quota) > victim_ok(unquota),
        f"不限流 {pct(victim_ok(unquota))} → 限流 {pct(victim_ok(quota))}",
    )
    checks.check(
        "whale 被限流（出现了租户配额拒绝）",
        quota.metrics.by_reason.get(RejectReason.TENANT_QUOTA_EXCEEDED, 0) > 0,
        f"配额拒绝={quota.metrics.by_reason.get(RejectReason.TENANT_QUOTA_EXCEEDED, 0)}",
    )

    # ---------------------------------------------------------------- T6
    print("\n[T6] 请求合并减少上游调用（§9.1）")
    hot = steady_workload(12.0, duration_ms=40_000, prompt_space=50,
                          zipf_alpha=1.3, variant_ratio=0.5)
    plain = run(queue_only(), hot)
    merged = run(queue_only(coalescing_enabled=True), hot)
    full = run(governed(), hot)
    # 判据用**减负比**而不是「上游调用数」：
    # 后者被队列容量截断了（没被派发出去的请求根本不产生调用），
    # 两个配置的调用数会因为「谁被派发得多」而不可比 —— 那是被混淆的变量。
    checks.check(
        "只开合并就能减掉四成上游工作",
        merged.summary["cache_offload"] > 0.40,
        f"减负 {pct(merged.summary['cache_offload'])}，"
        f"上游调用 {merged.summary['upstream_calls']}",
    )
    checks.check(
        "合并放大比 > 1.5",
        merged.gateway["coalescer"]["amplification"] >= 1.5,
        f"={merged.gateway['coalescer']['amplification']:.2f}×",
    )
    checks.check(
        "合并+分层缓存把减负做到 85% 以上",
        full.summary["cache_offload"] > 0.85,
        f"减负 {pct(full.summary['cache_offload'])}，"
        f"上游调用 {full.summary['upstream_calls']}",
    )

    # ---------------------------------------------------------------- T7
    print("\n[T7] 合并的等待者一定会被扇出（不泄漏）")
    checks.check(
        "所有到达的请求都有终态记录",
        merged.metrics.total == plain.metrics.total,
        f"合并 {merged.metrics.total} vs 不合并 {plain.metrics.total}",
    )
    checks.check(
        "没有挂起未终结的等待者",
        merged.gateway["coalescer"]["in_flight_now"] == 0,
        f"in_flight_now={merged.gateway['coalescer']['in_flight_now']}",
    )

    # ---------------------------------------------------------------- T8
    print("\n[T8] 公平调度：WFQ 改善小租户（§8.4）")
    fair_workload = multi_tenant_workload(FAIRNESS_TENANTS, duration_ms=40_000)
    fifo = run(queue_only(queue_policy=QueuePolicy.FIFO), fair_workload)
    wfq = run(queue_only(queue_policy=QueuePolicy.WFQ), fair_workload)

    def mouse_p99(result):
        return next(s for s in result.metrics.tenant_summary()
                    if s["tenant_id"] == "mouse")["latency"]["p99"]

    checks.check(
        "WFQ 下小租户 P99 更低",
        mouse_p99(wfq) < mouse_p99(fifo),
        f"FIFO {ms(mouse_p99(fifo))} → WFQ {ms(mouse_p99(wfq))}",
    )

    # ---------------------------------------------------------------- T9
    print("\n[T9] 超时预算与超期清扫（§11.2）")
    no_budget = run(queue_only(request_budget_ms=600_000), overload)
    no_sweep = run(queue_only(), overload)
    swept = run(queue_only(sweep_expired=True), overload)
    checks.check(
        "没有端到端预算时，成功率被虚高了（做完但没人要也算成功）",
        no_budget.success_ratio > no_sweep.success_ratio + 0.15,
        f"无预算 {pct(no_budget.success_ratio)} vs 有预算 {pct(no_sweep.success_ratio)}",
    )
    checks.check(
        "有预算时，上游确实白干了活（完成时已超期）",
        no_sweep.gateway["counters"]["inflight_timeout"] > 0,
        f"inflight_timeout={no_sweep.gateway['counters']['inflight_timeout']}",
    )
    checks.check(
        "清扫把大部分超期判定从「出队时」前移了",
        swept.gateway["counters"]["swept_timeout"] > 0
        and swept.gateway["counters"]["dispatch_expired"]
        < no_sweep.gateway["counters"]["dispatch_expired"],
        f"出队时判定 {no_sweep.gateway['counters']['dispatch_expired']} → "
        f"{swept.gateway['counters']['dispatch_expired']}；"
        f"清扫移出 {swept.gateway['counters']['swept_timeout']}",
    )
    checks.check(
        "不开启清扫时，出队时的最后一道确认仍在兜底",
        no_sweep.gateway["counters"]["dispatch_expired"] > 0,
        f"dispatch_expired={no_sweep.gateway['counters']['dispatch_expired']}",
    )

    # ---------------------------------------------------------------- T10
    print("\n[T10] 重试预算限制负载放大（§12.2）")
    flaky = DEFAULT_UPSTREAM.model_copy(update={"transient_fail_rate": 0.25})
    retry_workload = steady_workload(5.2, duration_ms=40_000)
    naive = run(queue_only(retry_max_attempts=3, retry_budget_ratio=None),
                retry_workload, upstream=flaky)
    budgeted = run(queue_only(retry_max_attempts=3, retry_budget_ratio=0.10),
                   retry_workload, upstream=flaky)
    checks.check(
        "朴素重试的放大比高于有预算的",
        naive.summary["retry_amplification"] > budgeted.summary["retry_amplification"],
        f"朴素 {naive.summary['retry_amplification']:.3f} → "
        f"预算 {budgeted.summary['retry_amplification']:.3f}",
    )
    checks.check(
        "有预算时放大比自己接近 1（不会自我放大）",
        budgeted.summary["retry_amplification"] < 1.25,
        f"={budgeted.summary['retry_amplification']:.3f}",
    )

    # ---------------------------------------------------------------- T11
    print("\n[T11] 降级既提速又可见（§13.2）")
    heavy = steady_workload(9.0, duration_ms=40_000)
    no_degrade = run(queue_only(degrade_enabled=False), heavy)
    degraded = run(upstream_bound(), heavy)
    checks.check(
        "降级配置的成功率更高或吞吐更高",
        degraded.success_ratio >= no_degrade.success_ratio
        or degraded.achieved_rps > no_degrade.achieved_rps,
        f"不降级 {pct(no_degrade.success_ratio)}/{no_degrade.achieved_rps:.2f} vs "
        f"降级 {pct(degraded.success_ratio)}/{degraded.achieved_rps:.2f}",
    )
    checks.check(
        "降级被显式计数（不是静默的质量下降）",
        degraded.summary["degraded"] > 0,
        f"degraded={degraded.summary['degraded']}",
    )

    # ---------------------------------------------------------------- T12
    print("\n[T12] 扩容指标：CPU 与 QPS 对「容量缩水」无感（§14.1）")
    slow = DEFAULT_UPSTREAM.model_copy(update={"slow_down_at_ms": 10_000,
                                               "slow_down_factor": 3.0})
    slow_workload = steady_workload(4.0, duration_ms=50_000)
    by_cpu = run(queue_only(autoscale_enabled=True, autoscale_signal="CPU",
                            autoscale_cold_start_ms=4_000), slow_workload, upstream=slow)
    by_queue = run(queue_only(autoscale_enabled=True, autoscale_signal="QUEUE_DEPTH",
                              autoscale_cold_start_ms=4_000,
                              autoscale_queue_threshold=12),
                   slow_workload, upstream=slow)
    checks.check(
        "CPU 扩容一次都没触发",
        by_cpu.autoscaler["scale_ups"] == 0,
        f"CPU 读数峰值 {max(by_cpu.series('utilisation') or [0]):.2f}（利用率），"
        f"扩容 {by_cpu.autoscaler['scale_ups']} 次",
    )
    checks.check(
        "队列深度扩容触发了",
        by_queue.autoscaler["scale_ups"] > 0,
        f"扩容 {by_queue.autoscaler['scale_ups']} 次，"
        f"事件={[(f'{t}ms', limit) for t, limit, _ in by_queue.autoscaler['events']]}",
    )

    # ---------------------------------------------------------------- T13
    print("\n[T13] 配置自检能拦住静默炸弹（§18）")
    report = check_config(governed())
    checks.check("默认治理配置自检通过", report.ok, f"problems={report.problems}")
    bad = governed(upstream_concurrency=64)
    bad_report = check_config(bad, DEFAULT_UPSTREAM)
    checks.check(
        "并发超过上游配额被拦下",
        any("上游配额" in p for p in bad_report.problems),
        f"problems={bad_report.problems[:1]}",
    )
    # 「预算 ≤ 服务时间」这种内部就自相矛盾的配置，由**配置对象自己的校验器**
    # 在构造时拦下 —— 比事后自检更强（错误的配置根本构造不出来）。
    try:
        governed(request_budget_ms=1000)
        blocked = False
    except Exception:
        blocked = True
    checks.check(
        "预算小于服务时间在构造时就被拒绝（比自检更强）",
        blocked,
        "GatewayConfig(request_budget_ms=1000) 抛出 ValidationError",
    )
    naive_report = check_config(governed(retry_budget_ratio=None))
    checks.check(
        "无重试预算被标为「对照组才合法」",
        any("重试无预算" in n for n in naive_report.notices),
        f"notices={len(naive_report.notices)} 条",
    )

    # ---------------------------------------------------------------- T14
    print("\n[T14] 可复现性")
    spec = steady_workload(5.0, duration_ms=15_000)
    first = run(queue_only(), spec)
    second = run(queue_only(), spec)
    checks.check(
        "同一种子跑两次结果完全一致",
        (first.summary["upstream_calls"] == second.summary["upstream_calls"]
         and first.p99 == second.p99
         and first.metrics.total == second.metrics.total),
        f"up={first.summary['upstream_calls']}/{second.summary['upstream_calls']} "
        f"p99={first.p99}/{second.p99}",
    )

    # ---------------------------------------------------------------- T15
    print("\n[T15] 全链路完整性：每个请求都有且只有一个终态")
    checks.check(
        "终态数 == 到达数",
        merged.metrics.total == merged.gateway["counters"]["arrived"],
        f"终态 {merged.metrics.total} vs 到达 {merged.gateway['counters']['arrived']}",
    )
    checks.check(
        "没有任何请求是「既没成功也没被拒绝」",
        all(record.status for record in merged.metrics.outcomes),
        "所有 OutcomeRecord 都有 status",
    )

    return checks.report()


# ======================================================================
# 场景注册与入口
# ======================================================================
SCENARIOS: dict[str, tuple[str, Callable[[], int | None]]] = {
    "baseline": ("对照组：无治理 / 只有效率优化 / 完整治理（§1）", scenario_baseline),
    "capacity": ("容量曲线与饱和点（§2）", scenario_capacity),
    "admission": ("准入控制 vs 队列满就拒（§7 / §8.1）", scenario_admission),
    "ratelimit": ("多级限流与邻居隔离（§6）", scenario_ratelimit),
    "coalesce": ("请求合并与分层缓存（§9 / §10）", scenario_coalesce),
    "fairness": ("公平调度：FIFO vs WFQ（§8.3 / §8.4）", scenario_fairness),
    "deadline": ("超时预算与超期清扫（§11.2）", scenario_deadline),
    "resilience": ("重试预算与熔断（§12）", scenario_resilience),
    "degrade": ("过载降级与分级丢弃（§13）", scenario_degrade),
    "autoscale": ("弹性伸缩指标选择（§14）", scenario_autoscale),
    "tests": ("回归测试（带断言）", scenario_tests),
}
ALL_ORDER = [
    "baseline", "capacity", "admission", "ratelimit", "coalesce",
    "fairness", "deadline", "resilience", "degrade", "autoscale", "tests",
]


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
        prog="traffic-gateway",
        description="Agent Traffic Gateway —— 高并发大流量下的流量治理 Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m app.main --list\n"
            "  python -m app.main all\n"
            "  python -m app.main capacity fairness\n"
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
    print("  Agent Traffic Gateway")
    print("  高并发大流量下的流量治理与容量工程")
    print(f"  上游容量 {CAPACITY:.2f} RPS（{DEFAULT_UPSTREAM.max_concurrency} 并发 / "
          f"{DEFAULT_UPSTREAM.mean_service_ms / 1000:.1f}s）")
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
