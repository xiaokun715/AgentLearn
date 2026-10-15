"""装配层 —— 把各模块拼成一个可运行的排障子系统，并做启动自检。

对应说明书 §10.2 的项目结构：``models`` / ``evidence_store`` / ``hypothesis_manager``
/ ``workflow_planner`` / ``workflow_validator`` / ``workflow_executor`` /
``recovery_controller`` / ``experience_manager`` 在这里被接线。

**为什么值得写一个启动自检**（:func:`check_config`）：
排障系统的很多配置错误平时完全看不出来，只有真出故障那一刻才暴露 ——
比如动作池里声明了一个没注册工具的动作，等到生产上跑到那一步才发现「检查根本做不了」。
用几十毫秒的启动时间换掉这类静默炸弹，是划算的（这条经验借自第十二章的工具执行平台）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .agent.agent import TroubleshootingAgent
from .clock import Clock
from .domain.enums import Observation
from .domain.models import Budget
from .domain.policy import DEFAULT_PERMISSIONS
from .evidence.store import EvidenceStore
from .execution.executor import CheckpointStore, WorkflowExecutor
from .experience.manager import ExperienceManager
from .hypothesis.manager import HypothesisManager
from .llm import ScriptedLLM
from .planning.actions import ActionPool
from .planning.planner import DiagnosticPlanner
from .planning.validator import WorkflowValidator
from .recovery.controller import RecoveryController
from .sim import ALL_WORLDS, FaultWorld, SimulatedPlatform
from .tools import ToolContext, build_registry

DEFAULT_ACTION_CONFIG = Path(__file__).resolve().parent.parent / "configs" / "actions.yaml"


@dataclass
class PlatformConfig:
    """排障子系统的可调参数。"""

    budget: Budget = field(
        default_factory=lambda: Budget(
            max_steps=8,
            max_tool_calls=16,
            max_seconds=600.0,
            # 三级恢复阶梯（刷新 → 重新申请 → 重启）要走完，所以给到 3
            max_recovery_attempts=3,
            max_nodes_per_plan=6,
        )
    )
    permissions: set[str] = field(default_factory=lambda: set(DEFAULT_PERMISSIONS))
    action_config: Path | None = DEFAULT_ACTION_CONFIG
    #: 是否允许 LLM 提议计划（提议的东西一定要过校验器）
    use_llm_planner: bool = False
    #: ScriptedLLM 的行为模式（valid / cyclic / unauthorized / unknown_skill / ...）
    plan_style: str = "valid"
    #: 模式 C 是否组装并行分支（§6.2 的 DAG 生成）
    branching: bool = False
    #: 是否使用预定义模板（模式 A）。关掉它就能观察到纯动态诊断的行为差异
    use_templates: bool = True
    #: 检查点落盘目录（``None`` 表示只留在内存里）
    checkpoint_dir: Path | None = None

    @classmethod
    def for_demo(cls) -> PlatformConfig:
        return cls()


class Platform:
    """一次装配出来的排障子系统。"""

    def __init__(self, config: PlatformConfig, world: str = "env", clock: Clock | None = None) -> None:
        self.config = config
        self.clock = clock or Clock()
        self.world: FaultWorld = ALL_WORLDS[world]()
        self.simulation = SimulatedPlatform(self.world, clock=self.clock)

        # ---- 动作池（代码默认 + YAML 覆盖）------------------------------
        self.pool = (
            ActionPool.from_yaml(config.action_config)
            if config.action_config else ActionPool.default()
        )
        # ---- 工具与证据 --------------------------------------------------
        self.registry = build_registry()
        self.store = EvidenceStore()
        self.tool_ctx = ToolContext(platform=self.simulation, clock=self.clock)

        # ---- 规划 / 校验 / 执行 -------------------------------------------
        self.llm = ScriptedLLM(plan_style=config.plan_style)
        self.planner = DiagnosticPlanner(
            self.pool,
            permissions=config.permissions,
            llm=self.llm,
            branching=config.branching,
        )
        self.validator = WorkflowValidator(self.pool, permissions=config.permissions)
        self.executor = WorkflowExecutor(
            self.registry, self.pool, self.store, self.clock,
            CheckpointStore(config.checkpoint_dir),
        )
        self.recovery = RecoveryController(
            self.pool, self.registry, self.store, self.clock,
            permissions=config.permissions, budget=config.budget,
        )
        self.experience = ExperienceManager()
        #: 由 :func:`build_platform` 填入；直接构造 ``Platform`` 时为空报告
        self.config_report = ConfigReport()

    # ------------------------------------------------------------------
    def build_agent(self, **overrides) -> TroubleshootingAgent:
        kwargs = dict(
            pool=self.pool,
            store=self.store,
            executor=self.executor,
            planner=self.planner,
            validator=self.validator,
            recovery=self.recovery,
            experience=self.experience,
            tool_ctx=self.tool_ctx,
            clock=self.clock,
            budget=self.config.budget,
            llm=self.llm,
            use_llm_planner=self.config.use_llm_planner,
        )
        kwargs.update(overrides)
        return TroubleshootingAgent(**kwargs)

    def troubleshoot(self, **kwargs):
        """一行跑一次完整排障（便利方法）。"""
        return self.build_agent().troubleshoot(self.world.context, **kwargs)


# ======================================================================
# 启动自检（§10.2 的落地提醒 + 第十二章的「静默炸弹」经验）
# ======================================================================
@dataclass
class ConfigReport:
    """启动自检结论。

    区分 ``problems`` 与 ``notices`` 很重要：把「有问题」和「有情况」混成一锅，
    团队就会开始忽略所有告警 —— 那这个自检还不如没有。
    """

    problems: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_config(platform: Platform) -> ConfigReport:
    """检查装配是否自洽。"""
    report = ConfigReport()

    # 1. 动作池里每个动作都要有实现，否则跑到那一步才发现「检查做不了」
    for spec in platform.pool.all():
        if not platform.registry.has(spec.name):
            report.problems.append(f"动作 {spec.name} 在动作池里，但没有注册对应的工具实现")

    # 2. 注册了却没有声明的工具（可能是漏声明，也可能是废弃代码）
    declared = set(platform.pool.names())
    for name in platform.registry.names():
        if name not in declared:
            report.problems.append(
                f"工具 {name} 已注册但未在动作池中声明（缺少成本/风险/权限元数据）"
            )

    # 3. 似然表必须对每个假设的每个可能结果都有定义，且每行和为 1
    for spec in platform.pool.diagnostic():
        outcomes = [item.value for item in spec.declared_outcomes()]
        for key, row in spec.likelihoods.items():
            missing = [item for item in outcomes if item not in row]
            if missing:
                report.problems.append(
                    f"{spec.name} 对 {key} 缺少似然项 {missing} —— "
                    "查不到会被当成「无信息」，很容易掩盖配置错误"
                )
            total = sum(row.get(item, 0.0) for item in outcomes)
            if abs(total - 1.0) > 1e-6:
                report.problems.append(
                    f"{spec.name} 对 {key} 的似然和={total:.4f}，不是 1 "
                    "（未归一化的似然会让信息增益算错）"
                )
        # 「对所有假设似然相同」不是错误，而是**刻意的设计**：
        # 历史案例检索就是这样一个动作 —— 它有价值（提供先验与先例），
        # 但价值不在于区分根因。所以这里只提示，让读者注意到它不会被选中。
        distinct = {tuple(sorted(row.items())) for row in spec.likelihoods.values()}
        if spec.likelihoods and len(distinct) == 1:
            report.notices.append(
                f"{spec.name} 对所有假设的似然完全相同 -> 信息增益恒为 0，"
                "规划器不会为了「缩小不确定性」选它（这是刻意的）"
            )

    # 4. 恢复动作的分级与权限必须匹配（§7.1 的安全边界）
    for spec in platform.pool.recovery():
        if spec.tier.value in ("R3", "R4") and not spec.permission:
            report.problems.append(
                f"恢复动作 {spec.name} 是 {spec.tier.value}，但没有声明所需权限"
            )
        # R0 的口径是「查询日志、读取配置」，出现副作用就是分级错了
        if spec.tier.value == "R0" and spec.side_effect == "mutate":
            report.problems.append(
                f"{spec.name} 声明为 R0（只读）却会改状态 —— 分级错了"
            )
        # R1 允许低风险写入（例如建缺陷单），但必须显式承认风险
        if spec.tier.value == "R1" and spec.side_effect == "mutate":
            report.notices.append(
                f"{spec.name} 是 R1 但有副作用（{spec.side_effect}）—— "
                f"确认它属于「无副作用的状态刷新」那一类（risk={spec.risk}）"
            )
        if spec.permission and spec.permission not in platform.config.permissions:
            # 不是错误：R3/R4 的权限本来就不该出现在默认权限集里
            report.notices.append(
                f"{spec.name} 的权限 {spec.permission} 不在默认权限集内 —— "
                "符合分级预期（需要走审批）"
            )

    # 5. 预算必须容得下恢复阶梯，否则「阶梯升级」永远走不完
    longest = max(
        (len(_ladder(key)) for key in ("H_ENV", "H_TOOL")),
        default=0,
    )
    if platform.config.budget.max_recovery_attempts < longest:
        report.problems.append(
            f"max_recovery_attempts={platform.config.budget.max_recovery_attempts} 小于"
            f"最长恢复阶梯 {longest} 步 —— 升级路径会被预算截断"
        )

    # 6. 似然表覆盖的假设键必须在假设库里存在（拼错键 = 该假设永不被更新）
    from .domain.policy import HYPOTHESIS_LIBRARY

    for spec in platform.pool.diagnostic():
        for key in spec.likelihoods:
            if key not in HYPOTHESIS_LIBRARY:
                report.problems.append(f"{spec.name} 的似然表引用了不存在的假设键 {key}")

    # 7. 假设库里的 required_checks 必须真的存在
    for key, hypothesis in HYPOTHESIS_LIBRARY.items():
        for check in hypothesis.required_checks:
            if platform.pool.get(check) is None:
                report.problems.append(f"假设 {key} 要求检查 {check}，但动作池里没有这个动作")

    # 8. 观测枚举与似然表用的字符串键必须一致
    known = {item.value for item in Observation}
    for spec in platform.pool.diagnostic():
        for key, row in spec.likelihoods.items():
            unknown = set(row) - known
            if unknown:
                report.problems.append(
                    f"{spec.name}/{key} 的似然表使用了未知观测键 {sorted(unknown)}"
                )

    return report


def _ladder(key: str) -> list[str]:
    from .recovery.controller import RECOVERY_LADDER

    return RECOVERY_LADDER.get(key, [])


def build_platform(
    config: PlatformConfig | None = None,
    *,
    world: str = "env",
    clock: Clock | None = None,
    strict: bool = True,
) -> Platform:
    """装配 + 自检。``strict=True`` 时自检不通过直接抛异常。"""
    platform = Platform(config or PlatformConfig.for_demo(), world=world, clock=clock)
    report = check_config(platform)
    platform.config_report = report
    if not report.ok and strict:
        raise RuntimeError(
            "排障子系统配置自检未通过：\n  - " + "\n  - ".join(report.problems)
        )
    return platform


def describe_hypotheses(manager: HypothesisManager) -> list[str]:
    """把假设集合渲染成终端可读的几行。"""
    lines = []
    for item in manager.summary():
        lines.append(
            f"{item['key']:<12} {item['status']:<10} "
            f"prior={item['prior']:.2f} post={item['posterior']:.2f} "
            f"支持={item['supporting']} 反驳={item['contradicting']}"
        )
    return lines
