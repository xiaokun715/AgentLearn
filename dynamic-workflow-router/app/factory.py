"""装配层 —— 把池子、意图识别、路由、编排、执行拼成一个可运行的系统。

对应说明书 §16 的项目结构，以及 §15 的反模式清单 ——
:func:`check_config` 把其中能静态检查的几条变成启动时的断言。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .domain.enums import AgentHealth, RiskLevel, TemplateState
from .domain.models import SubAgentSpec
from .domain.policy import INTENT_LIBRARY, ROUTING
from .engine import WorkflowRouter
from .intent.recognizer import IntentRecognizer
from .llm import ScriptedLLM
from .pool.agents import AgentPool
from .pool.templates import TemplateStore, seed_templates
from .routing.engine import RoutingEngine
from .workflow.composer import CapabilityPlanner, LLMComposer
from .workflow.runtime import RuntimeOptions, WorkflowRuntime
from .workflow.validator import WorkflowValidator

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AGENT_CONFIG = ROOT / "configs" / "agents.yaml"


# ======================================================================
@dataclass
class PlatformConfig:
    """运行期配置。"""

    #: 是否预置种子模板（§13.1 冷启动）
    seed_templates: bool = True
    #: LLM 提议编排的风格（valid / missing_dependency / cyclic / ...）
    plan_style: str = "valid"
    #: 是否默认启用 LLM 提议路径
    use_llm_composer: bool = False
    #: 启动自检不通过时是否直接抛异常
    strict: bool = True
    #: 子 Agent 声明的 YAML 覆盖文件
    agent_config: Path | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def for_demo(cls) -> PlatformConfig:
        return cls()


# ======================================================================
class Platform:
    """一次装配出来的路由系统。"""

    def __init__(self, config: PlatformConfig | None = None) -> None:
        self.config = config or PlatformConfig.for_demo()
        self.llm = ScriptedLLM(plan_style=self.config.plan_style)

        self.pool = AgentPool()
        self._apply_agent_overrides()

        self.store = (
            seed_templates(self.pool) if self.config.seed_templates
            else TemplateStore(self.pool)
        )
        self.recognizer = IntentRecognizer(self.llm)
        self.router = RoutingEngine(self.pool, self.store)
        self.validator = WorkflowValidator(self.pool)
        self.runtime = WorkflowRuntime(self.pool)
        self.planner = CapabilityPlanner(self.pool)

        self.app = WorkflowRouter(
            pool=self.pool,
            store=self.store,
            recognizer=self.recognizer,
            router=self.router,
            validator=self.validator,
            runtime=self.runtime,
            planner=self.planner,
            llm_composer=LLMComposer(self.pool, self.llm),
            llm=self.llm,
        )

    # ------------------------------------------------------------------
    def _apply_agent_overrides(self) -> None:
        """``configs/agents.yaml`` 允许覆盖成本 / 健康度 —— 但不允许新增 Agent。

        与其它章节同一条纪律：**配置只能覆盖已声明的东西**。
        允许 YAML 凭空造一个 Agent，等于让运维改一次配置就能引入一个
        没有实现、没有数据流契约、没人测过的执行体。
        """
        path = self.config.agent_config or DEFAULT_AGENT_CONFIG
        if not path.exists():
            return
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, override in (raw.get("agents") or {}).items():
            spec = self.pool.get(name)
            if spec is None:
                raise KeyError(f"configs/agents.yaml 声明了池子里不存在的 Agent：{name}")
            for field_name, value in (override or {}).items():
                if field_name == "health":
                    self.pool.set_health(
                        name, AgentHealth(value),
                        factor=float((override or {}).get("health_factor", 1.0)),
                    )
                    continue
                if field_name == "health_factor":
                    continue
                if not hasattr(spec, field_name):
                    raise KeyError(f"Agent {name} 不支持覆盖字段 {field_name}")
                setattr(spec, field_name, value)

    # ------------------------------------------------------------------
    def run(self, request, *, runtime_options: RuntimeOptions | None = None,
            use_llm_composer: bool | None = None):
        return self.app.run(
            request,
            runtime_options=runtime_options,
            use_llm_composer=(
                self.config.use_llm_composer if use_llm_composer is None
                else use_llm_composer
            ),
        )

    def decide_fate(self, result, fate, **kwargs):
        return self.app.decide_fate(result, fate, **kwargs)


# ======================================================================
# 启动自检（§15 反模式的可执行版本）
# ======================================================================
@dataclass
class ConfigReport:
    problems: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_config(platform: Platform) -> ConfigReport:
    report = ConfigReport()
    pool = platform.pool

    # ---- 1. 意图要求的能力必须有人能提供 ------------------------------
    for spec in INTENT_LIBRARY.values():
        for capability in spec.required_capabilities:
            providers = pool.by_capability(capability)
            if not providers:
                report.problems.append(
                    f"意图 {spec.name} 需要能力 {capability}，但池子里没有任何 Agent 提供它 —— "
                    "这个意图永远无法被完成"
                )
        # ---- 2. 意图的 estimated_steps 必须与能力数自洽 ---------------
        if spec.estimated_steps < 1:
            report.problems.append(f"意图 {spec.name} 的 estimated_steps 非法：{spec.estimated_steps}")

    # ---- 3. 每个 Agent 的 reads 必须有人产出（数据流可闭合）----------
    producible = {key for spec in pool.all() for key in spec.writes}
    for spec in pool.all():
        for key in spec.reads:
            if key not in producible:
                report.problems.append(
                    f"Agent {spec.name} 需要 {key!r}，但池子里没有任何 Agent 产出它 —— "
                    "依赖它的模板全部无法闭合数据流"
                )

    # ---- 4. 有副作用的 Agent 必须是高风险 ----------------------------
    for spec in pool.all():
        if spec.side_effect and spec.risk is not RiskLevel.HIGH:
            report.problems.append(
                f"Agent {spec.name} 有副作用但风险等级是 {spec.risk.value} —— "
                "风险标签会误导路由与审批"
            )
        if spec.side_effect and spec.risk is RiskLevel.HIGH and not spec.reads:
            report.notices.append(f"Agent {spec.name} 是高风险的写操作，确认它有前置校验")

    # ---- 5. 模板库里的模板必须可执行 ---------------------------------
    library = platform.store.audit_health()
    if library.broken:
        report.notices.append(
            f"模板库里有 {len(library.broken)} 条模板不可执行（引用的 Agent 缺失或下线），"
            f"已标记为 BROKEN：{library.broken}"
        )

    # ---- 6. 权重必须满足「覆盖优先于意图」 ---------------------------
    if ROUTING.w_coverage <= ROUTING.w_intent:
        report.problems.append(
            "ROUTING 里 coverage 权重不高于 intent 权重 —— 会让覆盖不全的候选靠意图匹配胜出"
        )

    # ---- 7. 澄清阈值不能高于「满覆盖直连」的得分 ---------------------
    full_direct = (
        ROUTING.w_coverage * 1.0 + ROUTING.w_intent * 1.0 + ROUTING.w_reliability * 1.0
    )
    if ROUTING.clarify_threshold > full_direct:
        report.problems.append(
            f"clarify_threshold={ROUTING.clarify_threshold} 高于满覆盖直连的理论最高分 "
            f"{full_direct} —— 任何请求都会被送去澄清"
        )

    # ---- 8. 池子健康度提示 -------------------------------------------
    health = pool.health_report()
    if health.get(AgentHealth.OFFLINE.value):
        report.notices.append(
            f"池子里有 {health[AgentHealth.OFFLINE.value]} 个 Agent 处于 OFFLINE"
        )

    # ---- 9. 模板标签完整性 -------------------------------------------
    for template in platform.store.all():
        if not template.tags.author:
            report.problems.append(f"模板 {template.template_id} 缺少生成人标签")
        if not template.tags.intent:
            report.problems.append(f"模板 {template.template_id} 缺少意图标签")

    # ---- 10. DRAFT 模板不应留在库里 ----------------------------------
    drafts = [t.template_id for t in platform.store.all()
              if t.tags.state is TemplateState.DRAFT]
    if drafts:
        report.notices.append(f"库里有未定稿的模板：{drafts}")

    return report


def build_platform(config: PlatformConfig | None = None) -> Platform:
    config = config or PlatformConfig.for_demo()
    platform = Platform(config)
    report = check_config(platform)
    platform.config_report = report
    if not report.ok and config.strict:
        raise RuntimeError(
            "动态 Workflow 路由系统自检未通过：\n  - " + "\n  - ".join(report.problems)
        )
    return platform


__all__ = [
    "Platform",
    "PlatformConfig",
    "ConfigReport",
    "check_config",
    "build_platform",
    "SubAgentSpec",
]
