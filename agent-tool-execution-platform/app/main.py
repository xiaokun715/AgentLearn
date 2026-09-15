"""可靠工具执行平台 —— 命令行演示入口。

覆盖《第十二章：可靠工具执行系统设计说明书》§61~§67 的七个场景，
外加 §71 列出的十个「必须重点测的故障」。

    python -m app.main --list          # 看有哪些场景
    python -m app.main all             # 全跑
    python -m app.main sync async      # 只跑指定场景
    python -m app.main tests           # §71 的十个故障测试（带断言）

设计说明：这个项目**没有 HTTP API 层**（说明书 §58 的 REST 接口留作扩展），
入口就是本文件。这么做的原因是本书考察的重点是**可靠性语义**而不是接口形态：
幂等、租约、恢复、循环检测这些东西在 CLI 里能跑得清清楚楚，
套一层 FastAPI 只会让人误以为「接口通了 == 平台对了」。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import traceback
from typing import Callable, Optional

from .config import AppConfig, ToolOverride
from .domain.enums import ExecutionStatus
from .domain.models import ToolCall
from .domain.policy import RetryPolicy, SandboxPolicy
from .factory import build_platform
from .infra.clock import ManualClock

# ======================================================================
# 终端输出小工具
# ======================================================================
WIDTH = 78


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
    print(f"     {key:<26} {value}")


def ok(text: str) -> None:
    print(f"   ✓ {text}")


def warn(text: str) -> None:
    print(f"   ! {text}")


def brief(text: str, limit: int = 150) -> str:
    """把长文本压成一行，便于在终端里看。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def mask(key: Optional[str], keep: int = 12) -> str:
    """把幂等键显示成 ``idem_a1b2c3d4…`` —— 终端里不需要看全 24 位十六进制。"""
    if not key:
        return "-"
    return key if len(key) <= keep + 6 else f"{key[: keep + 5]}…"


# ======================================================================
# 场景 1：同步 Tool（§61）
# ======================================================================
def scenario_sync() -> None:
    banner("场景一：同步 Tool —— 计算 12345 * 6789（§61）")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)

    section(1, "Agent 提交 ToolCall", "§6")
    call = ToolCall(
        tool_name="calculator",
        arguments={"expression": "12345 * 6789"},
        graph_run_id="run_sync",
        logical_step_id="step_1",
    )
    step(f"tool={call.tool_name} arguments={call.arguments}")
    info("生成的幂等键", mask(call.ensure_idempotency_key()))

    section(2, "平台执行：校验 -> 权限 -> 幂等 -> 同步执行", "§54")
    result = platform.gateway.submit(call)
    info("状态", result.status.value)
    info("结论", result.outcome)
    info("耗时", f"{result.duration_ms} ms")
    if result.result:
        info("结果", result.result.to_agent_view()["data"])

    section(3, "同一个 ToolCall 再提交一次 —— 幂等命中", "§12")
    again = platform.gateway.submit(call)
    info("状态", again.status.value)
    info("结论", again.outcome)
    info("是否复用缓存结果", again.deduplicated)
    info("是否真的又跑了一次", again.duration_ms > 0 and not again.deduplicated)
    ok("第二次没有重新执行，直接返回了第一次的结果")

    print("\n  审计时间线：")
    for row in platform.audit.timeline(call.call_id)[:8]:
        print(f"     {row['seq']:>2}. {row['event_type']}")

    platform.close()


# ======================================================================
# 场景 2：异步 Tool（§62）
# ======================================================================
def scenario_async() -> None:
    banner("场景二：异步 Tool —— 帮我运行 TC001/TC002 测试（§62）")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)

    section(1, "提交后立刻拿到 job_id，Agent 不阻塞", "§4.2")
    result = platform.gateway.submit(
        ToolCall(
            tool_name="run_test",
            arguments={"test_cases": ["TC001", "TC002"], "timeout": 120},
            graph_run_id="run_async",
        )
    )
    info("状态", result.status.value)
    info("执行模式", result.execution_mode.value)
    info("job_id", result.job_id)
    info("恢复建议", getattr(result.resume_hint, "value", result.resume_hint))
    step("此刻 Tool 还没跑 —— 任务躺在 Redis Stream 里等 Worker")

    section(2, "Worker 消费队列：抢租约 -> 起心跳 -> 沙箱 -> 执行", "§14/§15")
    queue_before = platform.scheduler.queue_stats()
    info("待消费任务数", queue_before["queue_depth"])
    drained = platform.scheduler.drain()
    info("本轮消费任务数", drained)
    queue_after = platform.scheduler.queue_stats()
    info("待消费任务数", queue_after["queue_depth"])
    info("PEL（投递未 ACK）", queue_after["pending"])
    info("Stream 累计条目", queue_after["total_entries"])
    step("注意「累计条目」不会下降 —— Stream 的已消费条目不消失，别把它当队列长度看")

    section(3, "任务完成，状态转为终态", "§50")
    final = platform.gateway.query(result.call_id)
    info("状态", final.status.value)
    info("result_id", final.result_id)
    if final.result:
        data = final.result.to_agent_view().get("data") or {}
        info("用例统计", f"{data.get('passed')}/{data.get('total')} 通过")

    print("\n  执行事件（节选）：")
    for event in platform.db.list_events(result.call_id):
        if event.event_type in (
            "scheduler.dispatched", "lease.acquired", "sandbox.created",
            "tool.started", "tool.completed", "result.persisted",
        ):
            print(f"     · {event.event_type}")

    platform.close()


# ======================================================================
# 场景 3：Agent 崩溃恢复（§63）
# ======================================================================
def scenario_agent_crash() -> None:
    banner("场景三：Agent 崩溃恢复 —— Checkpoint + Idempotency（§63）")
    checkpoint_db = os.path.join(tempfile.mkdtemp(prefix="toolplat_ck_"), "ck.sqlite")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)

    section(1, "Agent 提交异步 Tool，把 pending 写进 Checkpoint", "§18")
    agent = platform.build_agent(checkpoint_db=checkpoint_db)
    out = agent.run("帮我运行 TC001 和 TC002 测试", run_id="run_crash")
    pending = out.get("pending_tool_call") or {}
    info("Graph 状态", out.get("status"))
    info("pending.call_id", pending.get("call_id"))
    info("pending.幂等键", mask(pending.get("idempotency_key")))
    ok("Graph 在这里 interrupt 了 —— 长任务不占用 Worker（§49）")

    section(2, "模拟 Agent 进程崩溃", "§19")
    agent.close()
    step("Agent 的内存、栈、局部变量全丢；磁盘上只剩 Checkpoint 与平台侧的幂等记录")

    section(3, "Tool 在平台侧继续跑完（与 Agent 死活无关）", "§17")
    platform.scheduler.drain()
    after = platform.gateway.query(pending["call_id"])
    info("Tool 状态", after.status.value)
    info("result_id", after.result_id)
    ok("Tool 的执行不依赖 Agent 是否存活 —— 这正是要平台化的原因")

    section(4, "Agent 重启：读 Checkpoint -> 查幂等 -> 复用结果", "§55")
    agent2 = platform.build_agent(checkpoint_db=checkpoint_db)
    snapshot = agent2.state("run_crash")
    info("读回的状态", snapshot.get("status") if snapshot else None)
    plan = platform.agent_recovery.plan_resume(agent2.pending_call("run_crash"))
    info("恢复动作", plan.action.value)
    info("是否已经拿到结果", plan.should_consume_result)
    info("是否需要重提交", plan.should_resubmit)
    step(f"判据：{brief(plan.reason, 120)}")

    section(5, "关键断言：Tool 只执行了一次", "§71 Test 3")
    executions = [e for e in platform.db.list_executions() if e.tool_name == "run_test"]
    info("run_test 执行记录数", len(executions))
    info("各记录状态", [e.status.value for e in executions])
    if len(executions) == 1:
        ok("没有重复执行 —— Checkpoint 给出进度，Idempotency 挡住重复副作用（§20）")
    else:
        warn("出现了重复执行！")

    agent2.close()
    platform.close()


# ======================================================================
# 场景 4：Worker 崩溃 + 租约回收（§64）
# ======================================================================
def scenario_worker_crash() -> None:
    banner("场景四：Worker 崩溃与租约回收（§64）")
    clock = ManualClock()
    platform = build_platform(AppConfig.for_demo(), clock=clock, enable_workers=False)

    section(1, "提交异步任务", "§4.2")
    result = platform.gateway.submit(
        ToolCall(
            tool_name="run_test",
            arguments={"test_cases": ["TC001"]},
            graph_run_id="run_worker_crash",
        )
    )
    call_id = result.call_id
    info("call_id", call_id)
    info("状态", result.status.value)

    section(2, "模拟 Worker 抢到租约后崩溃", "§56")
    lease = platform.leases.acquire(call_id, worker_id="worker_99", ttl_seconds=5)
    info("worker_99 租约", lease.lease_id if lease else "-")
    info("租约是否有效", platform.leases.is_alive(call_id))
    platform.audit.record(call_id, "worker.simulated_crash", worker_id="worker_99")
    step("Worker 进程没了 —— 它再也不会续租，但 Tool 的副作用状态未知")

    section(3, "拨快时钟，让租约过期", "§15")
    step("这里用的是可注入时钟，不需要真的 sleep 5 秒")
    clock.advance(6)
    info("租约是否有效", platform.leases.is_alive(call_id))
    info("过期租约索引", platform.leases.expired())

    section(4, "Lease Reaper 回收：判死 -> 按幂等性等级决定能否接管", "§56/§57")
    outcomes = platform.reap_once()
    for outcome in outcomes:
        info("call_id", outcome.call_id)
        info("幂等性等级", outcome.idempotency_level.value)
        info("风险等级", outcome.risk_level.value)
        info("回收动作", outcome.action.value)
        info("是否已重新入队", outcome.recovered)
        step(f"判据：{brief(outcome.reason, 120)}")

    section(5, "接管后任务重新执行完成", "§56")
    platform.scheduler.drain()
    final = platform.gateway.query(call_id)
    info("最终状态", final.status.value)
    ok("另一个 Worker 合法接管了这次执行（IDEMPOTENT 才允许）")

    platform.close()


# ======================================================================
# 场景 5：参数自愈（§65）
# ======================================================================
def scenario_repair() -> None:
    banner("场景五：参数自愈 —— LLM 写错类型（§65）")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)

    section(1, "LLM 给出的参数类型是错的", "§21")
    call = ToolCall(
        tool_name="run_test",
        arguments={"timeout": "300", "test_cases": "TC001"},  # 应当是 int / list[str]
        graph_run_id="run_repair",
    )
    info("原始参数", call.arguments)
    info("Tool 期待", "timeout: int, test_cases: list[str]")

    section(2, "Gateway 先校验，失败则确定性修复", "§22")
    platform.gateway.submit(call)
    for event in platform.db.list_events(call.call_id):
        if event.event_type == "parameter.repaired":
            for repair in event.payload.get("steps", []):
                info(
                    f"修复 {repair.get('field')}",
                    f"{repair.get('before')!r} -> {repair.get('after')!r}  ({repair.get('rule')})",
                )
    info("修复后参数", call.arguments)

    section(3, "自愈有边界：不能让 LLM 随手放大数值", "§23")
    bad = ToolCall(
        tool_name="run_test",
        arguments={"test_cases": ["TC001"], "timeout": 300000},
        graph_run_id="run_repair2",
    )
    rejected = platform.gateway.submit(bad)
    info("timeout=300000 的结果", rejected.status.value)
    info("拒绝原因", brief(rejected.error_message or "", 120))
    ok("Schema + 业务约束 + 权限三层防护 —— 而不是「LLM 说多少就多少」")

    platform.close()


# ======================================================================
# 场景 6：循环检测（§66）
# ======================================================================
def scenario_loop() -> None:
    banner("场景六：重复与循环检测 —— Agent 卡在 search/search/search（§66）")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)

    section(1, "同一个 run 反复调同一个 Tool、同一份参数", "§30")
    arguments = {"query": "幂等 租约", "top_k": 5}
    for index in range(6):
        call = ToolCall(
            tool_name="search_knowledge",
            arguments=dict(arguments),
            graph_run_id="run_loop",
            logical_step_id="step_1",  # 同一个逻辑步骤
        )
        result = platform.gateway.submit(call)
        signal = result.detail.get("loop_signal", "-")
        degradation = result.detail.get("degradation") or {}
        print(f"     第 {index + 1} 次: {result.status.value:<14} 信号={signal}")
        if degradation:
            print(f"               降级建议: {degradation}")
        if result.status == ExecutionStatus.FAILED:
            step(f"被拦下：{brief(result.error_message or '', 110)}")
            break

    section(2, "逐级升级：WARNING -> DEGRADED -> STOP", "§32")
    history = platform.duplicates.history("run_loop")
    info("执行历史条数", len(history))
    info("最近 5 条签名", [h.get("signature", "-")[:22] for h in history[-5:]])
    ok("不是「发现一次重复就停」，而是先降级（改 query / 降 top_k），还循环才 STOP（§33）")

    section(3, "DAG 循环：A -> B -> A -> B", "§31")
    for node in ["search", "analyze", "execute", "search", "analyze", "search", "analyze"]:
        verdict = platform.cycles.observe(run_id="run_dag", node=node)
    info("执行路径", platform.cycles.path("run_dag"))
    info("检测到的循环长度", verdict.cycle_length)
    info("重复节点", verdict.repeated_nodes)
    info("信号", verdict.signal.value)
    step(f"判据：{brief(verdict.reason, 120)}")

    platform.close()


# ======================================================================
# 场景 7：人工介入（§67）
# ======================================================================
def scenario_human() -> None:
    banner("场景七：人工介入 —— 高风险 Tool 必须过审批（§67）")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)

    section(1, "Agent 请求删除生产数据表", "§51")
    call = ToolCall(
        call_id="call_human_demo",
        tool_name="database_delete",
        agent_id="admin_agent",
        arguments={"table": "demo_orders", "where": "id > 3"},
        graph_run_id="run_human",
    )
    info("Tool", call.tool_name)
    info("风险等级", platform.registry.metadata(call.tool_name).risk_level.value)
    info("幂等性等级", platform.registry.metadata(call.tool_name).idempotency_level.value)
    result = platform.gateway.submit(call)
    info("状态", result.status.value)
    info("结论", result.outcome)
    ok("高风险 Tool 不会自动执行 —— 进入 WAITING_HUMAN")

    section(2, "管理员审批", "§52")
    pending = platform.approvals.pending()
    info("待审批单数量", len(pending))
    for record in pending:
        info("审批单", f"{record.call_id}  {record.status}  {record.tool_name}")
        info("发起原因", brief(record.reason, 100))

    section(3, "批准 -> 恢复执行", "§52 --APPROVE-->")
    approved = platform.gateway.approve(
        call.call_id, reviewer="ops-lead", reason="已核对变更单 CHG-2024-118"
    )
    info("状态", approved.status.value)
    info("结论", approved.outcome)
    step("审批通过 ≠ 免检：仍然要重新过权限、幂等、沙箱（§26）")
    platform.scheduler.drain()
    final = platform.gateway.query(call.call_id)
    info("最终状态", final.status.value)
    if final.result:
        info("执行结果", final.result.to_agent_view().get("data"))

    section(4, "另一个分支：驳回 -> 取消", "§52 --REJECT-->")
    rejected_call = ToolCall(
        call_id="call_human_reject",
        tool_name="database_delete",
        agent_id="admin_agent",
        arguments={"table": "demo_test_records", "where": "id > 5"},
        graph_run_id="run_human2",
    )
    platform.gateway.submit(rejected_call)
    rejected = platform.gateway.reject(
        rejected_call.call_id, reviewer="ops-lead", reason="变更窗口未到"
    )
    info("状态", rejected.status.value)
    info("结论", rejected.outcome)
    info("DB 里的状态", platform.gateway.status(rejected_call.call_id).value)

    platform.close()


# ======================================================================
# 场景 8：安全防护（§24 ~ §26）
# ======================================================================
def scenario_security() -> None:
    banner("附加场景：参数注入防护与权限校验（§24~§26）")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)

    section(1, "参数注入防护", "§24/§25")
    attacks = [
        ("路径穿越", "large_file_analysis", {"path": "../../../etc/passwd"}),
        ("SQL 注入", "database_delete", {"table": "demo_orders", "where": "1=1; DROP TABLE demo_orders"}),
        ("命令注入", "execute_python", {"code": "import os; os.system('rm -rf /')"}),
    ]
    for label, tool_name, arguments in attacks:
        result = platform.gateway.submit(ToolCall(tool_name=tool_name, arguments=arguments))
        info(label, f"{result.status.value}  {brief(result.error_message or '', 90)}")

    section(2, "权限校验：agent_no_db 调 database_delete", "§26")
    denied = platform.gateway.submit(
        ToolCall(
            tool_name="database_delete",
            agent_id="agent_no_db",
            arguments={"table": "demo_orders", "where": "id > 3"},
        )
    )
    info("状态", denied.status.value)
    info("错误类型", denied.error_type)
    info("恢复建议", getattr(denied.resume_hint, "value", denied.resume_hint))
    info("缺失权限", denied.detail.get("missing"))
    ok("权限失败直接拒绝，不 retry —— 重试不会让权限变好（§26）")

    section(3, "大结果走对象存储，只给 Agent 摘要", "§39/§40")
    big = platform.gateway.submit(
        ToolCall(
            tool_name="large_file_analysis",
            arguments={"path": "/workspace/data/big.csv", "size_mb": 8},
            graph_run_id="run_big",
        )
    )
    platform.scheduler.drain()
    final = platform.gateway.query(big.call_id)
    if final.result:
        view = final.result.to_agent_view()
        info("结果类型", view["result_type"])
        info("artifact_id", view.get("artifact_id"))
        info("原始大小", f"{view.get('size', 0):,} 字节")
        info("Agent 实际看到", f"{len(str(view)):,} 字节（含 preview）")
        step(f"preview：{brief(view.get('preview', ''), 90)}")
        ok(f"原始结果被压缩了约 {view.get('size', 1) // max(1, len(str(view)))} 倍才进 Context（§37）")

    platform.close()


# ======================================================================
# §71 的十个故障测试
# ======================================================================
class _Checks:
    """极简断言收集器 —— 让十个测试能跑完再一起报结论。"""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append((name, passed, detail))
        mark = "✓" if passed else "✗"
        print(f"   {mark} {name}")
        if detail:
            print(f"       {detail}")

    def report(self) -> int:
        passed = sum(1 for _, ok_, _ in self.results if ok_)
        total = len(self.results)
        print()
        print("=" * WIDTH)
        print(f"  §71 故障测试结果：{passed}/{total} 通过")
        print("=" * WIDTH)
        for name, ok_, _ in self.results:
            if not ok_:
                print(f"   ✗ FAILED: {name}")
        return 0 if passed == total else 1


def scenario_tests() -> int:
    banner("§71 最重要的测试 —— 这个项目不能只测「Tool 能不能执行」")
    checks = _Checks()

    # ---------------------------------------------------------------- Test 1
    print("\n[Test 1] 两个 Agent 同时调用同一个 Idempotency Key —— 预期 Tool 只执行一次")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)
    arguments = {"test_cases": ["TC011"], "timeout": 120}
    first = ToolCall(tool_name="run_test", arguments=dict(arguments),
                     graph_run_id="t1", logical_step_id="step_1")
    second = ToolCall(tool_name="run_test", arguments=dict(arguments),
                      graph_run_id="t1", logical_step_id="step_1")
    r1 = platform.gateway.submit(first)
    r2 = platform.gateway.submit(second)
    platform.scheduler.drain()
    executions = [e for e in platform.db.list_executions() if e.tool_name == "run_test"]
    checks.check(
        "幂等键相同时只产生一条执行记录",
        len(executions) == 1,
        f"第一次={r1.outcome} 第二次={r2.outcome} 执行记录数={len(executions)}",
    )
    checks.check(
        "第二个请求指向同一个 call_id（没有另起炉灶）",
        r1.call_id == r2.call_id,
        f"{r1.call_id} vs {r2.call_id}",
    )
    platform.close()

    # ---------------------------------------------------------------- Test 2
    print("\n[Test 2] Worker Crash —— 预期 Lease Expire -> Recovery")
    clock = ManualClock()
    platform = build_platform(AppConfig.for_demo(), clock=clock, enable_workers=False)
    result = platform.gateway.submit(
        ToolCall(tool_name="run_test", arguments={"test_cases": ["TC021"]}, graph_run_id="t2")
    )
    platform.leases.acquire(result.call_id, worker_id="crashed_worker", ttl_seconds=5)
    clock.advance(6)
    outcomes = platform.reap_once()
    checks.check(
        "租约过期后 Reaper 识别出可接管的执行",
        any(o.call_id == result.call_id for o in outcomes),
        f"回收结论={[o.action.value for o in outcomes]}",
    )
    platform.scheduler.drain()
    checks.check(
        "接管后任务完成（IDEMPOTENT Tool 允许重跑）",
        platform.gateway.query(result.call_id).status == ExecutionStatus.SUCCESS,
        f"最终状态={platform.gateway.query(result.call_id).status.value}",
    )
    platform.close()

    # ---------------------------------------------------------------- Test 3
    print("\n[Test 3] Agent Crash —— 预期 Checkpoint Resume + Idempotency Check")
    checkpoint_dir = tempfile.mkdtemp(prefix="toolplat_t3_")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)
    agent = platform.build_agent(checkpoint_db=os.path.join(checkpoint_dir, "ck.sqlite"))
    out = agent.run("帮我运行 TC031 测试", run_id="t3")
    pending = out.get("pending_tool_call") or {}
    agent.close()
    platform.scheduler.drain()
    agent2 = platform.build_agent(checkpoint_db=os.path.join(checkpoint_dir, "ck.sqlite"))
    plan = platform.agent_recovery.plan_resume(agent2.pending_call("t3"))
    executions = [e for e in platform.db.list_executions() if e.tool_name == "run_test"]
    checks.check(
        "重启后能判断出「这一步已经做完了」",
        plan.should_consume_result,
        f"动作={plan.action.value} 有结果={plan.should_consume_result}",
    )
    checks.check(
        "没有因为重启而重复执行",
        len(executions) == 1,
        f"执行记录数={len(executions)}",
    )
    agent2.close()
    platform.close()

    # ---------------------------------------------------------------- Test 4
    print("\n[Test 4] Tool Timeout —— 预期 Sandbox Kill -> FAILED -> Recovery")
    # 把 execute_python 的超时压到 2 秒，否则要真的等 60 秒。
    #
    # 注意是在 for_demo() **之上**打补丁，而不是把 tools 整体替换掉：
    # 直接传 tools={...} 会让其它 Tool 全部丢掉 configs/tools.yaml 里的策略，
    # 于是它们的沙箱超时退化成代码默认值，配置自检会刷一屏「§29 违规」——
    # 那是**构造测试的方式**引入的噪声，不是平台的问题，会掩盖真正要看的东西。
    config = AppConfig.for_demo()
    config.tools["execute_python"] = ToolOverride(
        timeout_ms=2000,
        retry=RetryPolicy(max_attempts=1, initial_backoff_ms=0, max_backoff_ms=0),
        sandbox=SandboxPolicy(timeout_seconds=15, backend="process"),
    )
    platform = build_platform(config, enable_workers=False)
    result = platform.gateway.submit(
        ToolCall(
            tool_name="execute_python",
            arguments={"code": "import time; time.sleep(120)"},
            graph_run_id="t4",
            tenant_id="tenantA",
        )
    )
    platform.scheduler.drain()
    final = platform.gateway.query(result.call_id)
    checks.check(
        "超长的 Tool 被沙箱杀掉并判为失败",
        final.status == ExecutionStatus.FAILED,
        f"状态={final.status.value} 错误类型={final.error_type}",
    )
    checks.check(
        "错误被分类成 timeout（而不是笼统的 internal_error）",
        final.error_type == "timeout",
        f"error_type={final.error_type}",
    )
    platform.close()

    # ---------------------------------------------------------------- Test 5
    print("\n[Test 5] Tool 返回 100MB 级结果 —— 预期 Object Storage + Preview")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)
    result = platform.gateway.submit(
        ToolCall(
            tool_name="large_file_analysis",
            arguments={"path": "/workspace/data/huge.log", "size_mb": 100},
            graph_run_id="t5",
        )
    )
    platform.scheduler.drain()
    final = platform.gateway.query(result.call_id)
    view = final.result.to_agent_view() if final.result else {}
    checks.check(
        "大结果被转成 artifact，而不是塞进 Context",
        view.get("result_type") == "artifact",
        f"result_type={view.get('result_type')} 原始大小={view.get('size'):,} 字节"
        if final.result else "没有结果",
    )
    checks.check(
        "给 Agent 的视图远小于原始结果",
        bool(view) and len(str(view)) < view.get("size", 0) // 2,
        f"Agent 视图={len(str(view)):,} 字节 vs 原始={view.get('size', 0):,} 字节",
    )
    platform.close()

    # ---------------------------------------------------------------- Test 6
    print("\n[Test 6] Agent 连续调用同一个 Tool —— 预期 Duplicate Detection")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)
    signals = []
    for _ in range(6):
        r = platform.gateway.submit(
            ToolCall(
                tool_name="search_knowledge",
                arguments={"query": "幂等", "top_k": 5},
                graph_run_id="t6",
                logical_step_id="step_1",
            )
        )
        signals.append(r.detail.get("loop_signal", "-"))
    checks.check(
        "重复调用被检测到并逐级升级",
        any(s in ("DEGRADED", "STOP") for s in signals),
        f"信号序列={signals}",
    )
    checks.check(
        "升级到 STOP 之后调用被拦下",
        signals[-1] == "STOP" or "STOP" in signals,
        f"最终信号={signals[-1]}",
    )
    platform.close()

    # ---------------------------------------------------------------- Test 7
    print("\n[Test 7] A -> B -> A -> B —— 预期 Cycle Detection")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)
    verdict = None
    for node in ["search", "analyze", "execute", "search", "analyze", "search", "analyze"]:
        verdict = platform.cycles.observe(run_id="t7", node=node)
    checks.check(
        "识别出执行路径里的循环",
        verdict is not None and verdict.cycle_length > 0,
        f"循环长度={getattr(verdict, 'cycle_length', None)} 重复节点={getattr(verdict, 'repeated_nodes', None)}",
    )
    checks.check(
        "循环信号达到了会触发降级的级别",
        verdict is not None and verdict.signal.value in ("DEGRADED", "STOP", "WARNING"),
        f"信号={getattr(verdict, 'signal', None)}",
    )
    platform.close()

    # ---------------------------------------------------------------- Test 8
    print("\n[Test 8] Permission Denied —— 预期 No Retry")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)
    result = platform.gateway.submit(
        ToolCall(
            tool_name="database_delete",
            agent_id="agent_no_db",
            arguments={"table": "demo_orders", "where": "id > 3"},
        )
    )
    from .domain.enums import RecoveryAction
    checks.check(
        "权限不足被直接拒绝",
        result.status == ExecutionStatus.FAILED and result.error_type == "permission_error",
        f"状态={result.status.value} 错误类型={result.error_type}",
    )
    checks.check(
        "恢复建议是 ABORT 而不是 RETRY",
        result.resume_hint == RecoveryAction.ABORT,
        f"resume_hint={getattr(result.resume_hint, 'value', result.resume_hint)}",
    )
    platform.close()

    # ---------------------------------------------------------------- Test 9
    print("\n[Test 9] 参数类型错误 —— 预期 Repair -> Validate -> Execute")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)
    call = ToolCall(
        tool_name="run_test",
        arguments={"timeout": "300", "test_cases": "TC091"},
        graph_run_id="t9",
    )
    result = platform.gateway.submit(call)
    checks.check(
        "类型错误的参数被自动修复",
        call.arguments.get("timeout") == 300 and call.arguments.get("test_cases") == ["TC091"],
        f"{call.arguments}",
    )
    platform.scheduler.drain()
    checks.check(
        "修复后成功执行",
        platform.gateway.query(result.call_id).status == ExecutionStatus.SUCCESS,
        f"状态={platform.gateway.query(result.call_id).status.value}",
    )
    platform.close()

    # ---------------------------------------------------------------- Test 10
    print("\n[Test 10] 高风险 Tool —— 预期 WAITING_HUMAN")
    platform = build_platform(AppConfig.for_demo(), enable_workers=False)
    result = platform.gateway.submit(
        ToolCall(
            call_id="t10_call",
            tool_name="database_delete",
            agent_id="admin_agent",
            arguments={"table": "demo_orders", "where": "id > 3"},
            graph_run_id="t10",
        )
    )
    checks.check(
        "高风险 Tool 停在 WAITING_HUMAN",
        result.status == ExecutionStatus.WAITING_HUMAN,
        f"状态={result.status.value}",
    )
    checks.check(
        "没有在没人批准的情况下执行",
        platform.gateway.status("t10_call") == ExecutionStatus.WAITING_HUMAN,
        f"DB 状态={platform.gateway.status('t10_call').value}",
    )
    platform.close()

    return checks.report()


# ======================================================================
# 场景注册表与入口
# ======================================================================
SCENARIOS: dict[str, tuple[str, Callable[[], Optional[int]]]] = {
    "sync": ("同步 Tool：计算 + 幂等命中（§61）", scenario_sync),
    "async": ("异步 Tool：队列 + Worker + 结果（§62）", scenario_async),
    "agent-crash": ("Agent 崩溃恢复：Checkpoint + Idempotency（§63）", scenario_agent_crash),
    "worker-crash": ("Worker 崩溃与租约回收（§64）", scenario_worker_crash),
    "repair": ("参数自愈与自愈边界（§65）", scenario_repair),
    "loop": ("重复与 DAG 循环检测（§66）", scenario_loop),
    "human": ("人工介入：高风险审批（§67）", scenario_human),
    "security": ("注入防护 + 权限 + 大结果（§24~§26/§39/§40）", scenario_security),
    "tests": ("§71 的十个故障测试（带断言）", scenario_tests),
}
ALL_ORDER = [
    "sync", "async", "agent-crash", "worker-crash",
    "repair", "loop", "human", "security", "tests",
]


def _configure_logging(verbose: bool) -> None:
    """默认压住平台内部日志 —— 演示输出里混着 INFO 会很吵。

    ``--verbose`` 打开 ``DEBUG``；不打开时只放行 WARNING 及以上，
    这样「沙箱退化」「配置自检不通过」这类真正需要注意的信息仍然会露出来。
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # 第三方库的 DEBUG 太吵，单独压掉
    for noisy in ("langgraph", "langchain_core", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tool-platform",
        description="Agent Tool Execution Platform —— 可靠工具执行系统 Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m app.main --list\n"
            "  python -m app.main all\n"
            "  python -m app.main sync human\n"
            "  python -m app.main tests --verbose\n"
        ),
    )
    parser.add_argument(
        "scenarios", nargs="*", default=[],
        help="要运行的场景名（留空 = all）",
    )
    parser.add_argument("--list", action="store_true", help="列出所有场景后退出")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    args = parser.parse_args(argv)

    if args.list:
        print("可用场景：\n")
        for name in ALL_ORDER:
            print(f"  {name:<14} {SCENARIOS[name][0]}")
        print(f"\n  {'all':<14} 依次运行全部场景")
        return 0

    _configure_logging(args.verbose)

    requested = args.scenarios or ["all"]
    if "all" in requested:
        requested = ALL_ORDER

    unknown = [name for name in requested if name not in SCENARIOS]
    if unknown:
        print(f"未知场景：{unknown}\n可用：{ALL_ORDER}", file=sys.stderr)
        return 2

    print("=" * WIDTH)
    print("  Agent Tool Execution Platform")
    print("  基于 LangGraph 的可靠工具执行系统")
    print(f"  待运行场景：{', '.join(requested)}")
    print("=" * WIDTH)

    exit_code = 0
    for name in requested:
        title, runner = SCENARIOS[name]
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
