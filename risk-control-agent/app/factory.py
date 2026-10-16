"""装配层 —— 把在线链路、工具网关、调查 Agent、离线评估拼成一个系统。

对应说明书 §3.1 的总体架构。**两条链路共用同一份数据、但不共用控制流**：

    RiskPlatform
      ├── online/     在线链路：确定性、毫秒级、不调查
      ├── gateway     工具网关：Agent 唯一的出口，权限/范围/预算/审计
      ├── agent/      Agent 链路：动态、秒级、只建议不执行
      └── evaluation/ 离线链路：验证假设、评估策略、产出候选

自检（:func:`check_config`）检查的每一条都对应 §7 的一个反模式 ——
尤其是「Agent 是否真的拿不到执行权限」这一条：它是本设计最核心的约束，
必须由代码在启动时断言，而不是靠文档承诺。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .agent.fixed_workflow import FixedWorkflowRunner
from .agent.investigator import Investigator
from .agent.planner import CONTEXT_TOOLS, DISCRIMINATIVE_POWER, InvestigationPlanner
from .domain.enums import RiskLevel, ToolKind
from .domain.policy import (
    AGENT_GRANTS,
    APPROVAL_GRANTS,
    BUDGET,
    HYPOTHESIS_LIBRARY,
    ONLINE_RULES,
    STRATEGY_POLICY,
    TOOL_INDEX,
    InvestigationBudget,
)
from .evaluation.offline import OfflineValidator, PatternDiscoverer, StrategyAnalyzer
from .gateway.tool_gateway import ToolGateway
from .llm import ScriptedLLM
from .online.decision import DecisionEngine
from .online.features import FeatureComputer
from .online.risk_model import RiskModel
from .online.rules import RuleEngine
from .sim import (
    RiskWorld,
    world_device_farm_evasion,
    world_false_positive,
    world_group_fraud,
    world_sparse_data,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOOL_CONFIG = ROOT / "configs" / "tools.yaml"

WORLD_BUILDERS = {
    "group_fraud": world_group_fraud,
    "false_positive": world_false_positive,
    "sparse_data": world_sparse_data,
    "device_farm": world_device_farm_evasion,
}


@dataclass
class PlatformConfig:
    #: 调查预算（§4.4）
    max_tool_calls: int = 10
    max_cost: float = 12.0
    #: LLM 提议风险模式的风格（演示用）
    pattern_style: str = "valid"
    strict: bool = True
    tool_config: Path | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def for_demo(cls) -> PlatformConfig:
        return cls()


# ======================================================================
class RiskPlatform:
    """一次装配出来的风控 Agent 平台。"""

    def __init__(self, world_name: str = "group_fraud",
                 config: PlatformConfig | None = None) -> None:
        self.config = config or PlatformConfig.for_demo()
        self.world: RiskWorld = WORLD_BUILDERS[world_name]()

        # ---- 在线链路 --------------------------------------------------
        self.features = FeatureComputer(self.world)
        self.rules = RuleEngine(self.world)
        self.model = RiskModel()
        self.decision = DecisionEngine()

        # ---- 工具网关 --------------------------------------------------
        self.tools = dict(TOOL_INDEX)
        self._apply_tool_overrides()
        self.gateway = ToolGateway(self.tools, set(AGENT_GRANTS),
                                   approval_grants=APPROVAL_GRANTS)

        # ---- Agent 链路 ------------------------------------------------
        self.llm = ScriptedLLM(style=self.config.pattern_style)
        self.planner = InvestigationPlanner(self.tools)
        # **配置里的预算必须真的传下去。** 早先版本这里漏传，调查器一直用的是
        # 模块级的默认预算 —— 于是 self.config.max_tool_calls 只被自检读到、
        # 从来没有生效过。这类「配置看起来生效了、其实没有」的问题，
        # 只有在跑一个**本来会超预算**的场景时才会暴露。
        budget = InvestigationBudget(
            max_tool_calls=self.config.max_tool_calls,
            max_cost=self.config.max_cost,
            max_elapsed_ms=BUDGET.max_elapsed_ms,
            max_barren_rounds=BUDGET.max_barren_rounds,
        )
        self.investigator = Investigator(self.world, self.gateway, self.planner,
                                         budget=budget, llm=self.llm)
        self.fixed_workflow = FixedWorkflowRunner(self.world, self.gateway)

        # ---- 离线链路 --------------------------------------------------
        self.discoverer = PatternDiscoverer(self.world)
        self.validator = OfflineValidator(self.world)
        self.strategy = StrategyAnalyzer(self.world)

    # ------------------------------------------------------------------
    def _apply_tool_overrides(self) -> None:
        """``configs/tools.yaml`` 只能覆盖已有工具的元数据，**不能新增工具**。

        允许 YAML 凭空造一个工具，等于让运维改一次配置就能给 Agent
        开一个新的数据出口 —— 而这个出口没有经过任何权限评审。
        """
        path = self.config.tool_config or DEFAULT_TOOL_CONFIG
        if not path.exists():
            return
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, override in (raw.get("tools") or {}).items():
            spec = self.tools.get(name)
            if spec is None:
                raise KeyError(f"configs/tools.yaml 声明了不存在的工具：{name}")
            for key, value in (override or {}).items():
                if not hasattr(spec, key):
                    raise KeyError(f"工具 {name} 不支持覆盖字段 {key}")
                setattr(spec, key, value)

    # ==================================================================
    def evaluate_online(self, event):
        """跑一次在线决策链路。"""
        features = self.features.compute(event)
        hits = self.rules.evaluate(event, features)
        score = self.model.score(event, features)
        return self.decision.decide(event, features, hits, score)

    def alert_from(self, online_decision, event, prefix: str = "") -> object:
        return self.decision.to_alert(online_decision, event, prefix)

    def make_alert(self, entity_id: str, reason: str, *, rule_ids=None,
                   model_score: float = 0.0):
        """直接构造一个告警（用于演示不需先跑在线链路的场景）。"""
        from .domain.models import Alert

        profile_device = ""
        for device_id, device in self.world.devices.items():
            if entity_id in device.accounts:
                profile_device = device_id
                break
        profile = self.world.users.get(entity_id)
        return Alert(
            rule_ids=list(rule_ids or []),
            entity_type="user",
            entity_id=entity_id,
            reason=reason,
            model_score=model_score,
            context={
                "device_id": profile_device,
                "ip": next((ip for ip, accounts in self.world.ips.items()
                            if entity_id in accounts), ""),
                "refund_ratio": round(profile.refund_ratio_7d, 4) if profile else 0.0,
            },
        )

    def investigate(self, alert):
        return self.investigator.investigate(alert)

    def run_fixed_workflow(self, alert):
        return self.fixed_workflow.investigate(alert)

    # ==================================================================
    def snapshot(self) -> dict:
        return {
            "world": self.world.name,
            "users": len(self.world.users),
            "devices": len(self.world.devices),
            "cases": len(self.world.cases),
            "unavailable_sources": sorted(self.world.unavailable_sources),
            "tools": len(self.tools),
            "gateway": self.gateway.snapshot(),
        }


# ======================================================================
# 启动自检（§7 反模式的可执行版本）
# ======================================================================
@dataclass
class ConfigReport:
    problems: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_config(platform: RiskPlatform) -> ConfigReport:
    report = ConfigReport()

    # ---- 1. 最核心的一条：Agent 不能有执行/改配置的权限 ----------------
    forbidden = {"risk:enforce", "risk:admin"}
    leaked = AGENT_GRANTS & forbidden
    if leaked:
        report.problems.append(
            f"Agent 权限集里出现了 {sorted(leaked)} —— "
            "调查 Agent 拥有处置权或改规则的权限，是本设计的根本禁忌（§3.2）"
        )

    # ---- 2. 有副作用的工具不能是 LOW（那是「没分类」，不是「很安全」）----
    #
    # 注意判据是 **LOW**，不是「必须 HIGH」。写工单是良性的写操作，
    # MEDIUM 是它的正确等级；把「有副作用」一律抬成 HIGH，会让审批列表
    # 里挤满不值得人看的东西 —— 那才是真正危险的（人会开始无脑批准）。
    for spec in platform.tools.values():
        if spec.side_effect and spec.risk is RiskLevel.LOW:
            report.problems.append(
                f"工具 {spec.name} 有副作用但风险等级是 LOW —— "
                "这通常意味着它没有被分类过，而不是它很安全"
            )
        if spec.side_effect and not spec.permission:
            report.problems.append(f"工具 {spec.name} 会改世界，但没有声明权限要求")
        if spec.kind is ToolKind.ADMIN and spec.name in APPROVAL_GRANTS:
            report.problems.append(
                f"{spec.name} 是 ADMIN 类工具，却被放进了可审批通道 —— "
                "改风控配置不应该存在「Agent 申请一下就放行」的路径"
            )

    # ---- 4. 假设库与工具的能力必须对得上 ------------------------------
    targeted = {key for spec in platform.tools.values() for key in spec.targets}
    for key in HYPOTHESIS_LIBRARY:
        if key not in targeted and key != "H_INSUFFICIENT_DATA":
            report.problems.append(
                f"假设 {key} 没有任何工具能区分 —— "
                "它会被规划器永远留在 PENDING，最后变成一条「未检验」的记录"
            )

    # ---- 5. 每条在线规则引用的特征必须真的会被算出来 --------------------
    feature_names = set(platform.features.compute(
        _probe_event(platform)).values)
    for rule in ONLINE_RULES:
        if rule.feature not in feature_names:
            report.problems.append(
                f"规则 {rule.rule_id} 依赖特征 {rule.feature}，"
                "但特征层不会产出它 —— 这条规则永远不会命中"
            )

    # ---- 6. 规划器：区分力系数必须覆盖所有非上下文工具 ------------------
    for spec in platform.tools.values():
        if spec.targets and spec.name not in DISCRIMINATIVE_POWER:
            report.notices.append(
                f"工具 {spec.name} 没有声明区分力系数，将按默认 0.5 参与排序"
            )
        if spec.name in CONTEXT_TOOLS and spec.targets:
            report.problems.append(
                f"上下文工具 {spec.name} 声明了 targets —— "
                "它就会同时参与假设区分，语义变得含混"
            )

    # ---- 7. 调查预算必须容得下最短的有效路径 ---------------------------
    min_path = 3      # 账号 → 设备 → 案例 是最短的能形成双来源支持的路径
    if platform.config.max_tool_calls < min_path:
        report.problems.append(
            f"max_tool_calls={platform.config.max_tool_calls} 小于最短有效路径 "
            f"{min_path} 步 —— 所有调查都会以「证据不足」结束"
        )

    # ---- 8. 策略候选的必填字段必须包含漏报影响 -------------------------
    if "expected_fn_delta" not in STRATEGY_POLICY.required_fields:
        report.problems.append(
            "策略候选的必填字段里没有 expected_fn_delta —— "
            "会让「只报误报下降」的候选蒙混过关（§6.3）"
        )

    # ---- 9. 提示（不是问题）-------------------------------------------
    unavailable = platform.world.unavailable_sources
    if unavailable:
        report.notices.append(
            f"本世界的 {sorted(unavailable)} 数据源不可用 —— "
            "调查会走到「证据不足」，这是**预期行为**，不是故障"
        )
    if platform.world.truth:
        report.notices.append(
            "本世界带有裁判视角的 ground truth，仅用于场景脚本判定结论是否正确"
        )

    return report


def _probe_event(platform: RiskPlatform):
    from .domain.models import Event

    any_user = next(iter(platform.world.users), "probe")
    device = next((d for d, dev in platform.world.devices.items()
                   if any_user in dev.accounts), "")
    return Event(event_type="claim_coupon", user_id=any_user, device_id=device)


def build_platform(world_name: str = "group_fraud",
                   config: PlatformConfig | None = None) -> RiskPlatform:
    config = config or PlatformConfig.for_demo()
    platform = RiskPlatform(world_name, config)
    report = check_config(platform)
    platform.config_report = report
    if not report.ok and config.strict:
        raise RuntimeError(
            "风控 Agent 平台自检未通过：\n  - " + "\n  - ".join(report.problems)
        )
    return platform


__all__ = ["RiskPlatform", "PlatformConfig", "ConfigReport", "check_config",
           "build_platform", "WORLD_BUILDERS"]
