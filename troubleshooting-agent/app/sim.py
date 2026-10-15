"""模拟的 AI4Test 被测平台 —— 提供排障 Agent 要面对的「真实世界」。

说明书 §10.1 把职责分得很清楚：

    Agent    负责整体决策
    Skill    负责具体诊断能力
    Tool     执行真实的数据查询、日志读取、环境检查等操作

本模块就是 **Tool 那一层背后的世界**。它同时承担两个角色：

1. **故障现场**：每个 :class:`FaultWorld` 描述一次真实的失败（日志、配置、环境、
   工具记录、历史案例），并且有一个**只有它自己知道的**根因 ``root_cause_key``。
   排障 Agent 看不到这个字段 —— 它只能通过诊断工具一步一步把证据捞出来。
2. **可变更的环境**：恢复动作会真的改变世界状态（``env_ready``），复测也真的
   会重新判定通过与否。这样「受控恢复 → 结果验证」才不是一句空话。

把世界做成可注入的，是为了让演示可复现：同一个根因跑两次，证据序列完全一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .clock import Clock
from .domain.enums import FailureCategory
from .domain.models import FailureContext, TraceEvent

#: 演示用的固定时间基准 —— 让缓存有效期判断有确定的答案
BASE_TIME = datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc)


# ======================================================================
# 轨迹构造小工具
# ======================================================================
def _event(
    seq: int,
    *,
    agent: str = "TestExecutionAgent",
    skill: str = "",
    event_type: str = "skill_call",
    parent: str | None = None,
    status: str = "OK",
    tool: str | None = None,
    error_code: str | None = None,
    message: str = "",
    duration_ms: int = 0,
    metadata: dict | None = None,
) -> TraceEvent:
    event_id = f"EVENT_{seq:03d}"
    return TraceEvent(
        trace_id="TRACE_001",
        task_id="TASK_001",
        event_id=event_id,
        parent_event_id=parent,
        agent=agent,
        skill=skill,
        event_type=event_type,
        tool=tool,
        status=status,
        start_time=(BASE_TIME + timedelta(seconds=seq)).isoformat(timespec="seconds"),
        duration_ms=duration_ms,
        output_ref=f"artifact://outputs/{seq:03d}" if status == "OK" else None,
        error_code=error_code,
        message=message,
        metadata=metadata or {},
    )


# ======================================================================
# 故障世界
# ======================================================================
@dataclass
class FaultWorld:
    """一次故障现场的完整描述。``root_cause_key`` 是**裁判视角**的答案。"""

    name: str
    description: str
    context: FailureContext
    root_cause_key: str
    root_cause_text: str
    #: 各诊断工具会看到的发现项（空 = 该项正常）
    config_findings: list[str] = field(default_factory=list)
    log_findings: list[str] = field(default_factory=list)
    case_findings: list[str] = field(default_factory=list)
    tool_record_findings: list[str] = field(default_factory=list)
    #: 环境探针的确定性结论："READY" / "NOT_READY" / "INCONCLUSIVE"
    env_status: str = "READY"
    env_details: list[str] = field(default_factory=list)
    history_cases: list[dict] = field(default_factory=list)
    #: 哪些恢复动作能真的修好环境（其余动作执行了也没用）
    recoverable_by: list[str] = field(default_factory=list)
    #: 环境修好之后复测的结果
    rerun_when_healthy: str = "PASSED"
    #: 证据稀疏标记：所有诊断都返回 INCONCLUSIVE（用于演示 UNRESOLVED）
    sparse: bool = False


class SimulatedPlatform:
    """故障世界 + 它的可变状态。"""

    def __init__(self, world: FaultWorld, clock: Clock | None = None) -> None:
        self.world = world
        self.clock = clock
        self.env_ready = world.env_status == "READY"
        self.recovery_history: list[dict] = []
        self.rerun_history: list[dict] = []

    # ------------------------------------------------------------------
    # 环境探针
    # ------------------------------------------------------------------
    def probe(self) -> dict:
        if self.world.sparse:
            return {
                "status": "INCONCLUSIVE",
                "details": ["环境探针超时，未取得设备状态（接口 30s 无响应）"],
            }
        if self.env_ready:
            return {"status": "READY", "details": ["设备在线，资源已绑定，初始化完成"]}
        return {
            "status": "NOT_READY",
            "details": list(self.world.env_details)
            or ["设备未就绪：初始化流程未执行完成"],
        }

    def snapshot(self) -> dict:
        return {
            "env_ready": self.env_ready,
            "recovery_attempts": len(self.recovery_history),
            "at": BASE_TIME.isoformat(timespec="seconds"),
        }

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------
    def apply_recovery(self, action: str) -> dict:
        """执行一个恢复动作，返回执行结果（含恢复前后快照）。"""
        before = self.snapshot()
        fixes = action in self.world.recoverable_by
        if fixes:
            self.env_ready = True
        after = self.snapshot()
        record = {
            "action": action,
            "executed": True,
            "env_ready": self.env_ready,
            "before": before,
            "after": after,
            "note": (
                "环境恢复成功" if fixes and self.env_ready
                else "动作执行完毕，但环境状态没有变化"
            ),
        }
        self.recovery_history.append(record)
        return record

    # ------------------------------------------------------------------
    # 复测
    # ------------------------------------------------------------------
    def rerun(self) -> dict:
        """复测。

        **重要**：复测通过不能单独证明根因已经确定（说明书 §7.2 的原话）。
        这里如实返回结果，由调用方结合恢复前后的差异一起判断。
        """
        passed = self.env_ready and self.world.rerun_when_healthy == "PASSED"
        record = {
            "result": "PASSED" if passed else "FAILED",
            "env_ready": self.env_ready,
            "note": (
                "复测通过" if passed
                else ("环境仍未就绪，复测没有意义" if not self.env_ready else "环境已恢复，但复测仍然失败")
            ),
        }
        self.rerun_history.append(record)
        return record

    # ------------------------------------------------------------------
    # 缺陷单
    # ------------------------------------------------------------------
    def create_defect(self, summary: str) -> dict:
        return {"defect_id": "DEF_2026_0001", "summary": summary, "status": "OPEN"}


# ======================================================================
# 具体世界
# ======================================================================
def _srs_context(
    *,
    error_code: str | None,
    trace: list[TraceEvent],
    logs: list[str],
    config: dict,
    env_state: dict,
    cached: dict | None = None,
) -> FailureContext:
    return FailureContext(
        task_id="TASK_001",
        test_case_id="TC_SRS_001",
        failure_type=FailureCategory.UNKNOWN.value,
        expected_result={"measurement_status": "SUCCESS"},
        actual_result={"measurement_status": "FAILED"},
        software_version="V100R020C10",
        test_config=config,
        environment_state=env_state,
        execution_trace=trace,
        raw_logs=logs,
        artifacts=["artifact://outputs/srs_measurement_003"],
        related_cases=["CASE_SRS_2025_011"],
        observed_at=BASE_TIME + timedelta(minutes=5),
        cached_checks=cached or {},
    )


_COMMON_CONFIG = {"SrsOptimizationEnabled": True, "measurement_period_ms": 20}


def world_env_not_initialized() -> FaultWorld:
    """根因：环境没有正确初始化。

    这个世界的看点在于**失败节点不是根因节点**：``prepare_environment`` 自称成功
    （status=OK），但 ``verified=false`` 且没有 ``output_ref`` —— 环境初始化其实被跳过了。
    下游的测量采集因此报 ``ENV_NOT_READY``。排障 Agent 必须回溯到那个假成功节点。
    """
    trace = [
        _event(1, event_type="agent_start", message="开始执行 TC_SRS_001", duration_ms=12),
        _event(
            2,
            skill="prepare_environment",
            parent="EVENT_001",
            event_type="skill_call",
            duration_ms=430,
            message="环境准备返回成功",
            metadata={
                # ↓ 这三条就是「假成功」的结构性信号
                "verified": False,
                "expects_output": True,
                "result_complete": True,
            },
        ),
        _event(
            3,
            skill="execute_test_case",
            parent="EVENT_002",
            event_type="skill_call",
            duration_ms=1850,
            message="用例执行进入测量阶段",
            metadata={"precondition_violation": "prepare_environment"},
        ),
        _event(
            4,
            skill="collect_srs_measurement",
            parent="EVENT_003",
            event_type="tool_call",
            tool="srs_measurement_probe",
            status="FAILED",
            error_code="ENV_NOT_READY",
            duration_ms=300,
            message="采集失败：被测设备未绑定",
        ),
        _event(5, event_type="agent_end", parent="EVENT_004", duration_ms=5,
               message="任务以失败结束"),
    ]
    logs = [
        "2026-10-15 10:00:02 [INFO] prepare_environment: skip device binding (cached flag)",
        "2026-10-15 10:00:03 [WARN] device srs_dev_03 not bound to current environment",
        "2026-10-15 10:00:04 [ERROR] ERR_ENV_NOT_READY collect_srs_measurement aborted",
        "2026-10-15 10:00:04 [INFO] ERR_MEASUREMENT_ANOMALY measurement value = 0 (unexpected)",
    ]
    ctx = _srs_context(
        error_code="ENV_NOT_READY",
        trace=trace,
        logs=logs,
        config=dict(_COMMON_CONFIG),
        env_state={"status": "UNKNOWN"},
    )
    ctx.failure_type = FailureCategory.ENVIRONMENT.value
    return FaultWorld(
        name="env_not_initialized",
        description="环境未正确初始化：上游环境准备节点自称成功但实际跳过了设备绑定",
        context=ctx,
        root_cause_key="H_ENV",
        root_cause_text="环境准备节点声称成功但未执行设备绑定，导致测量采集失败",
        log_findings=[
            "ERR_ENV_NOT_READY：采集被中止",
            "WARN：设备 srs_dev_03 未绑定到当前环境",
            "ERR_MEASUREMENT_ANOMALY：测量值异常为 0",
        ],
        # 采集工具确实报错了 —— 但它是**受害者**：设备没绑定，采集当然会失败。
        # 这正是「工具执行异常」经常只是「环境异常」的二次现象。
        tool_record_findings=[
            "srs_measurement_probe 返回码非零（300ms 后中止）",
            "采集未产生有效测量样本",
        ],
        env_status="NOT_READY",
        env_details=[
            "设备 srs_dev_03 未绑定到当前环境",
            "环境初始化流程被跳过（prepare_environment 未产出绑定结果）",
        ],
        history_cases=[
            {"case_id": "CASE_SRS_2025_011", "similarity": 0.82,
             "note": "同类环境未初始化故障，历史上由重新申请环境解决"},
        ],
        recoverable_by=["reallocate_environment"],
    )


def world_config_mismatch() -> FaultWorld:
    """根因：配置与需求约束不一致。

    这个世界的轨迹**没有假成功节点**，错误码也不在规则表里，
    所以分类器掉进 LLM 兜底分支 —— 用来演示「规则命不中 != 规则失败」。
    """
    trace = [
        _event(1, event_type="agent_start", message="开始执行 TC_SRS_001"),
        _event(2, skill="load_test_config", parent="EVENT_001", duration_ms=40,
               metadata={"verified": True, "expects_output": True},
               message="配置加载成功"),
        _event(3, skill="execute_test_case", parent="EVENT_002", duration_ms=2100,
               message="用例执行完成，进入结果比对"),
        _event(4, skill="compare_expected_actual", parent="EVENT_003",
               event_type="tool_call", tool="assert_engine", status="FAILED",
               error_code="MEASUREMENT_MISMATCH", duration_ms=30,
               message="预期 measurement_status=SUCCESS，实际 FAILED"),
        _event(5, event_type="agent_end", parent="EVENT_004", duration_ms=4,
               message="任务以失败结束"),
    ]
    logs = [
        "2026-10-15 10:00:02 [INFO] config loaded: SrsOptimizationEnabled=true period=20ms",
        "2026-10-15 10:00:03 [INFO] measurement completed, status=FAILED",
        "2026-10-15 10:00:04 [ERROR] ASSERT_MISMATCH expected SUCCESS actual FAILED",
    ]
    ctx = _srs_context(
        error_code="MEASUREMENT_MISMATCH",
        trace=trace,
        logs=logs,
        config={
            "SrsOptimizationEnabled": True,
            "measurement_period_ms": 5,          # 需求要求 20ms —— 这里是真实的不一致
            "SrsOptimizationMode": "LEGACY",     # 需求要求 NORMAL
        },
        env_state={"status": "UNKNOWN"},
    )
    ctx.failure_type = FailureCategory.CONFIG.value
    return FaultWorld(
        name="config_mismatch",
        description="测试配置与需求约束不一致：测量周期与优化模式取值不符",
        context=ctx,
        root_cause_key="H_CONFIG",
        root_cause_text="measurement_period_ms=5 与需求要求的 20ms 不一致，且优化模式停留在 LEGACY",
        config_findings=[
            "measurement_period_ms=5，需求要求 20",
            "SrsOptimizationMode=LEGACY，需求要求 NORMAL",
        ],
        log_findings=[],        # 日志本身正常 —— 配置错误不会在日志里报错
        env_status="READY",
        history_cases=[
            {"case_id": "CASE_SRS_2025_027", "similarity": 0.61,
             "note": "配置类故障，历史上通过修正配置参数解决"},
        ],
        recoverable_by=[],
        rerun_when_healthy="FAILED",
    )


def world_case_expectation_wrong() -> FaultWorld:
    """根因：用例的预期结果定义错误。

    用来演示「LLM 的初始分类是错的，但证据驱动循环把它纠正过来了」——
    ScriptedLLM 会先猜 CONFIG，而检查结果会把 H_CASE 推上来。
    """
    trace = [
        _event(1, event_type="agent_start", message="开始执行 TC_SRS_001"),
        _event(2, skill="prepare_environment", parent="EVENT_001", duration_ms=420,
               metadata={"verified": True, "expects_output": True},
               message="环境准备完成并已校验"),
        _event(3, skill="execute_test_case", parent="EVENT_002", duration_ms=1900,
               message="用例执行完成"),
        _event(4, skill="compare_expected_actual", parent="EVENT_003",
               event_type="tool_call", tool="assert_engine", status="FAILED",
               error_code="ASSERT_MISMATCH", duration_ms=28,
               message="预期 measurement_status=SUCCESS，实际 FAILED"),
        _event(5, event_type="agent_end", parent="EVENT_004", duration_ms=3,
               message="任务以失败结束"),
    ]
    logs = [
        "2026-10-15 10:00:03 [INFO] environment bound to device srs_dev_11",
        "2026-10-15 10:00:04 [INFO] measurement completed value=1024 status=FAILED",
    ]
    ctx = _srs_context(
        error_code="ASSERT_MISMATCH",
        trace=trace,
        logs=logs,
        config=dict(_COMMON_CONFIG),
        env_state={"status": "READY"},
    )
    ctx.failure_type = FailureCategory.UNKNOWN.value
    return FaultWorld(
        name="case_expectation_wrong",
        description="用例预期结果定义错误：需求要求测量失败也算通过，用例写成了必须 SUCCESS",
        context=ctx,
        root_cause_key="H_CASE",
        root_cause_text="用例的预期结果与需求约束不一致（需求允许 FAILED 作为合法测量结果）",
        log_findings=[],
        case_findings=[
            "预期 measurement_status=SUCCESS，但需求 SRS-Req-114 允许 FAILED",
            "用例版本 v1（需求已更新到 v2，用例未同步）",
        ],
        env_status="READY",
        history_cases=[],
        recoverable_by=[],
        rerun_when_healthy="FAILED",
    )


def world_insufficient_evidence() -> FaultWorld:
    """证据稀疏：所有诊断都拿不到确定性结论 → 必须输出 ``UNRESOLVED``。

    说明书 §10.3 第 7 条：「无法定位时，应允许输出 UNRESOLVED，
    不能为了得到结论而强行选择一个根因。」这个世界的存在就是为了验证这一条。
    """
    trace = [
        _event(1, event_type="agent_start", message="开始执行 TC_SRS_001"),
        _event(2, skill="execute_test_case", parent="EVENT_001", status="FAILED",
               error_code="UNKNOWN_FAILURE", duration_ms=1800,
               message="用例执行失败，错误信息被截断"),
    ]
    ctx = _srs_context(
        error_code="UNKNOWN_FAILURE",
        trace=trace,
        logs=[],                       # 日志已被轮转
        config={},
        env_state={"status": "UNKNOWN"},
    )
    return FaultWorld(
        name="insufficient_evidence",
        description="证据不足：日志已轮转、环境探针超时、配置读取失败",
        context=ctx,
        root_cause_key="H_UNKNOWN",
        root_cause_text="（本次故障在预算内无法定位）",
        config_findings=[],
        log_findings=[],
        env_status="INCONCLUSIVE",
        recoverable_by=[],
        sparse=True,
    )


def world_device_hung() -> FaultWorld:
    """根因：环境异常，但只有 R3（重启设备）能修好。

    用来演示受控恢复的升级路径：R1 刷新没用 → R2 重新申请没用 →
    R3 需要人工审批 → 未获批准时停在 ``NEEDS_HUMAN``。
    """
    world = world_env_not_initialized()
    world.name = "device_hung"
    world.description = "设备挂死：只有重启设备（R3）能恢复，需要人工审批"
    world.root_cause_text = "被测设备 srs_dev_03 挂死，只有重启设备可以恢复"
    world.env_details = ["设备 srs_dev_03 无响应（心跳超时 300s）"]
    world.recoverable_by = ["restart_device"]
    return world


ALL_WORLDS = {
    "env": world_env_not_initialized,
    "config": world_config_mismatch,
    "case": world_case_expectation_wrong,
    "sparse": world_insufficient_evidence,
    "device-hung": world_device_hung,
}
