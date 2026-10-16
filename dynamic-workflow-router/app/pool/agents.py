"""Agent Pool —— 池子里的**子 Agent**（说明书 §4）。

池子承担两件事（也是需求 1 的两半）：

1. 登记所有子 Agent 的能力、数据流契约、成本、风险、健康度；
2. 为路由引擎与编排器提供**统一的查询接口** ——
   「谁能覆盖这组能力」「这个 agent 读什么写什么」「它现在可用吗」。

**为什么路由不能依赖 Agent 的名字**（§4.2）：名字是给人看的，
它会变（重命名、换供应商、拆成两个）。而 ``capabilities`` 是给机器看的，
它是路由与编排的唯一依据。本 Demo 里没有任何一处路由规则提到具体 agent 名。
"""
from __future__ import annotations

from ..domain.enums import AgentHealth, RiskLevel
from ..domain.models import SubAgentSpec

# ======================================================================
# §4.1 内置池子：三个域、14 个子 Agent
# ======================================================================
#: 判据写在这里而不是散落在路由规则里：池子知道「谁有什么能力」，
#: 路由只需要知道「这次要哪些能力」。
BUILTIN_AGENTS: list[SubAgentSpec] = [
    # ---------------- 测试排障域 ----------------
    SubAgentSpec(
        name="log_collector",
        description="采集原始日志（对象存储引用）",
        domain="troubleshooting",
        capabilities=["log_read"],
        reads=[],
        writes=["logs"],
        cost=1.0,
        latency_ms=300,
        risk=RiskLevel.LOW,
    ),
    SubAgentSpec(
        name="log_analyzer",
        description="解析日志，提取错误事件与时间线",
        domain="troubleshooting",
        capabilities=["log_analysis"],
        reads=["logs"],
        writes=["error_events"],
        cost=1.5,
        latency_ms=500,
        risk=RiskLevel.LOW,
    ),
    SubAgentSpec(
        name="env_prober",
        description="探测环境与设备状态",
        domain="troubleshooting",
        capabilities=["env_check"],
        reads=[],
        writes=["env_state"],
        cost=1.0,
        latency_ms=1200,
        risk=RiskLevel.LOW,
    ),
    SubAgentSpec(
        name="config_inspector",
        description="比对测试配置与需求约束",
        domain="troubleshooting",
        capabilities=["config_check"],
        reads=[],
        writes=["config_diff"],
        cost=1.0,
        latency_ms=400,
        risk=RiskLevel.LOW,
    ),
    SubAgentSpec(
        name="root_cause_analyst",
        description="综合全部证据给出根因假设",
        domain="troubleshooting",
        capabilities=["root_cause"],
        reads=["error_events", "env_state", "config_diff"],
        writes=["hypotheses"],
        cost=3.0,
        latency_ms=2000,
        risk=RiskLevel.LOW,
    ),
    SubAgentSpec(
        name="report_writer",
        description="把结论写成结构化报告",
        domain="troubleshooting",
        capabilities=["report"],
        reads=["hypotheses"],
        writes=["report"],
        cost=1.0,
        latency_ms=600,
        risk=RiskLevel.LOW,
    ),
    # ---------------- 代码审查域 ----------------
    SubAgentSpec(
        name="code_reader",
        description="读取代码或变更差异",
        domain="code_review",
        capabilities=["code_read"],
        reads=[],
        writes=["code"],
        cost=0.5,
        latency_ms=200,
        risk=RiskLevel.LOW,
    ),
    SubAgentSpec(
        name="security_reviewer",
        description="静态安全检查（注入、越权、密钥泄露）",
        domain="code_review",
        capabilities=["security_review"],
        reads=["code"],
        writes=["security_findings"],
        cost=2.0,
        latency_ms=900,
        risk=RiskLevel.LOW,
    ),
    SubAgentSpec(
        name="style_reviewer",
        description="代码风格与可读性检查",
        domain="code_review",
        capabilities=["style_review"],
        reads=["code"],
        writes=["style_findings"],
        cost=1.0,
        latency_ms=400,
        risk=RiskLevel.LOW,
    ),
    # ---------------- 数据分析域 ----------------
    SubAgentSpec(
        name="metric_query",
        description="查询时序指标",
        domain="data",
        capabilities=["metric_query"],
        reads=[],
        writes=["metrics"],
        cost=0.8,
        latency_ms=350,
        risk=RiskLevel.LOW,
    ),
    SubAgentSpec(
        name="trend_analyzer",
        description="分析指标趋势与拐点",
        domain="data",
        capabilities=["trend_analysis"],
        reads=["metrics"],
        writes=["trend"],
        cost=1.5,
        latency_ms=700,
        risk=RiskLevel.LOW,
    ),
    SubAgentSpec(
        name="chart_renderer",
        description="把趋势渲染成图",
        domain="data",
        capabilities=["chart"],
        reads=["metrics"],
        writes=["chart"],
        cost=0.5,
        latency_ms=150,
        risk=RiskLevel.LOW,
    ),
    # ---------------- 通用 ----------------
    SubAgentSpec(
        name="knowledge_searcher",
        description="检索规范与历史案例",
        domain="general",
        capabilities=["knowledge_search"],
        reads=[],
        writes=["references"],
        cost=1.0,
        latency_ms=450,
        risk=RiskLevel.LOW,
    ),
    SubAgentSpec(
        name="summarizer",
        description="对上下文做摘要",
        domain="general",
        capabilities=["summarize"],
        reads=[],
        writes=["summary"],
        cost=0.5,
        latency_ms=250,
        risk=RiskLevel.LOW,
    ),
    # ---------------- 高风险：用来演示「自动编排不能悄悄插进来」------------
    SubAgentSpec(
        name="config_applier",
        description="把配置修正**写回**测试环境",
        domain="troubleshooting",
        capabilities=["config_apply"],
        reads=["config_diff"],
        writes=["config_applied"],
        cost=2.0,
        latency_ms=1500,
        risk=RiskLevel.HIGH,
        side_effect=True,
    ),
]


class AgentPool:
    """子 Agent 的登记表。"""

    def __init__(self, agents: list[SubAgentSpec] | None = None) -> None:
        self._agents: dict[str, SubAgentSpec] = {}
        # **必须深拷贝**：``BUILTIN_AGENTS`` 是模块级的默认值，而池子是会被
        # 修改的（下线某个 Agent、调整成本）。直接 register 原对象的话，
        # 一个实例的 set_health 会改到所有实例的头上去 —— 表现是
        # 「单独跑某个测试能过，一起跑就挂」，而且挂的位置离原因很远。
        # 默认值必须是「每次取一份新的」，不能是「共享一份」。
        source = agents if agents is not None else BUILTIN_AGENTS
        for spec in source:
            self.register(spec.model_copy(deep=True))

    # ------------------------------------------------------------------
    def register(self, spec: SubAgentSpec) -> None:
        self._agents[spec.name] = spec

    def get(self, name: str) -> SubAgentSpec | None:
        return self._agents.get(name)

    def require(self, name: str) -> SubAgentSpec:
        spec = self._agents.get(name)
        if spec is None:
            raise KeyError(f"池子里没有子 Agent：{name}")
        return spec

    def all(self) -> list[SubAgentSpec]:
        return sorted(self._agents.values(), key=lambda item: item.name)

    def usable(self) -> list[SubAgentSpec]:
        return [item for item in self.all() if item.usable]

    # ------------------------------------------------------------------
    # 路由与编排要用的查询
    # ------------------------------------------------------------------
    def covering(self, capabilities: list[str]) -> list[tuple[SubAgentSpec, float]]:
        """谁能覆盖这组能力，覆盖度多少。返回按覆盖度降序的列表。

        **一个子 Agent 很少能覆盖多能力意图的全部** —— 这正是
        「多步意图天然需要编排」在本 Demo 里成为事实（而不是规定）的原因。
        """
        wanted = set(capabilities)
        if not wanted:
            return []
        scored: list[tuple[SubAgentSpec, float]] = []
        for spec in self.all():
            if not spec.usable:
                continue
            covered = wanted & set(spec.capabilities)
            if not covered:
                continue
            scored.append((spec, len(covered) / len(wanted)))
        scored.sort(key=lambda item: (-item[1], item[0].cost, item[0].name))
        return scored

    def by_capability(self, capability: str) -> list[SubAgentSpec]:
        """覆盖某个能力的全部可用 Agent —— 编排器在这里做选择（§8.1）。"""
        return [item for item in self.all() if item.usable and capability in item.capabilities]

    def agents_producing(self, key: str) -> list[SubAgentSpec]:
        return [item for item in self.all() if item.usable and key in item.writes]

    def cost_of(self, name: str) -> float:
        spec = self.get(name)
        return spec.cost * (2.0 - spec.health_factor) if spec else 0.0

    def latency_of(self, name: str) -> int:
        spec = self.get(name)
        if spec is None:
            return 0
        # 降级的 Agent 会更慢 —— 健康度必须体现在路由的成本估计里（§4.4）
        return int(spec.latency_ms / max(spec.health_factor, 0.1))

    def risk_of(self, name: str) -> RiskLevel:
        spec = self.get(name)
        return spec.risk if spec else RiskLevel.LOW

    def max_latency(self) -> int:
        return max((item.latency_ms for item in self.all()), default=1)

    # ------------------------------------------------------------------
    # 池子健康度（§4.4）
    # ------------------------------------------------------------------
    def set_health(self, name: str, health: AgentHealth, *, factor: float = 1.0) -> None:
        spec = self.require(name)
        spec.health = health
        spec.health_factor = factor if health is AgentHealth.DEGRADED else 1.0

    def health_report(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for spec in self.all():
            counts[spec.health.value] = counts.get(spec.health.value, 0) + 1
        return counts

    def snapshot(self) -> list[dict]:
        return [
            {
                "name": spec.name,
                "domain": spec.domain,
                "capabilities": spec.capabilities,
                # reads/writes 必须一起给出：模型提议编排时要用它写 needs/produces，
                # 少了这两个字段，提议出来的步骤就没有数据流契约（§8.2）
                "reads": spec.reads,
                "writes": spec.writes,
                "side_effect": spec.side_effect,
                "cost": spec.cost,
                "latency_ms": spec.latency_ms,
                "risk": spec.risk.value,
                "health": spec.health.value,
            }
            for spec in self.all()
        ]
