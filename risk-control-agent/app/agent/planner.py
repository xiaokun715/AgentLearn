"""调查规划器（说明书 §4.4）—— **本 Demo 与「固定工作流」的分界线就在这里**。

固定工作流：不管上一个工具查出了什么，都按同一张清单往下走。
规划器：**每一步都根据当前还活着的假设，重新算一次「查哪个最值」**。

## 打分方式

对每个候选工具：

    value(tool) = Σ_{h ∈ targets ∩ 活跃假设}  4·w_h·(1 − w_h)·discriminative_power(h,tool)
                − cost_penalty · cost

``4·w·(1−w)`` 是伯努利分布方差的形式，它**在 w=0.5 时最大**：

* 一个我们已经很确定的假设（w=0.9），再查它一次的价值很低 —— 方差只有 0.36；
* 一个完全没线索的假设（w=0.5），查一次能带来的信息最大。

这条设计让调查自然收敛：**证据越多，剩余可查的东西越少**，
不需要额外写「什么时候停」的规则。

## 三个细节

1. **被排除的工具不再被选中** —— 不是降分，而是从候选里去掉。
   假设已经被反驳了还去查它，是纯浪费；
2. **``preconditions`` 决定顺序约束** —— 图分析必须先有设备数据，
   这不是优化而是正确性（没有设备边，图分析跑出来是空的）；
3. **``search_similar_cases`` 与 ``query_risk_model_detail`` 的 ``targets`` 为空**，
   它们不区分假设，只在特定时机参与（见 :meth:`_context_tool_value`）。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import Hypothesis, ToolSpec

#: 领域经验给每个工具的「区分力」系数（0~1）。
#:
#: 它表达的是「这个工具查出来的结果，对该假设的判定有多强」——
#: 设备关联是团伙案最直接的证据，IP 聚集则弱得多（NAT、公司网络会误伤）。
DISCRIMINATIVE_POWER: dict[str, float] = {
    # 账号画像是标准起手式：它便宜、快，而且能一次性排除掉一大类猜测
    "query_account_profile": 1.0,
    # 设备图虽然是最强信号，但它覆盖的假设多不代表每一步都该先查它 ——
    # 系数表达的是「单位成本的区分力」，不是「信息量」
    "query_device_graph": 0.55,
    "query_transaction_history": 0.7,
    "query_coupon_usage": 0.5,
    "query_ip_cluster": 0.35,
    "run_graph_analysis": 0.8,
}

#: 上下文工具：它们不区分假设，只有在「有东西可对照」时才值得调用。
CONTEXT_TOOLS = {"search_similar_cases", "query_risk_model_detail"}

#: 同分时的领域推荐顺序（侦察性越强越靠前）。
PRIORITY = [
    "query_account_profile",
    "query_device_graph",
    "query_transaction_history",
    "query_ip_cluster",
    "query_coupon_usage",
    "run_graph_analysis",
    "search_similar_cases",
    "query_risk_model_detail",
]


@dataclass
class ScoredTool:
    spec: ToolSpec
    value: float
    active_targets: list[str]
    note: str = ""

    def explain(self) -> str:
        targets = ",".join(self.active_targets) or "（上下文工具）"
        return (f"{self.spec.name:<28} value={self.value:6.3f} "
                f"成本={self.spec.cost:.1f} 针对={targets}")


class InvestigationPlanner:
    """根据当前假设状态选择下一步查什么。"""

    def __init__(self, specs: dict[str, ToolSpec]) -> None:
        self.specs = specs

    # ==================================================================
    def rank(
        self,
        hypotheses: dict[str, Hypothesis],
        called: set[str],
        *,
        satisfied: set[str] | None = None,
        cost_penalty: float = 0.12,
    ) -> list[ScoredTool]:
        """``called`` 与 ``satisfied`` 是**两个不同的集合**，不能合并：

        * ``called``：已经调用过的工具 —— 不重复调用（同一个查询不会有新结果）；
        * ``satisfied``：**拿到过有效观测**的工具 —— 只有它们能满足前置条件。

        差别就出现在「设备图挂了」这个场景：调用过它、但没有拿到观测。
        这时依赖它的图分析**不该**被放行 —— 它会拿空数据跑出一个假结论，
        而这个假结论和真结论长得一模一样。
        """
        satisfied = satisfied if satisfied is not None else called
        active = {key: item for key, item in hypotheses.items() if item.active}
        scored: list[ScoredTool] = []

        for name, spec in self.specs.items():
            if name in called:
                continue
            if not self._preconditions_met(spec, satisfied):
                continue
            if name in CONTEXT_TOOLS:
                continue      # 上下文工具一律留到后面，见下

            targets = [key for key in spec.targets if key in active]
            if not targets:
                # 它针对的假设全部已被反驳 —— 查它没有任何信息可言
                continue
            power = DISCRIMINATIVE_POWER.get(name, 0.5)
            value = sum(4.0 * hypotheses[key].weight * (1.0 - hypotheses[key].weight)
                        for key in targets) * power
            value -= cost_penalty * spec.cost
            if value <= 0:
                continue
            scored.append(ScoredTool(spec, round(value, 4), targets))

        # **上下文工具只在没有「能区分假设的检查」可做时才上场。**
        #
        # 早先版本给它们一个固定的高分（例如「已有假设被支持时，案例检索
        # 值 1.0」），结果是：还有一个维度没查，规划器却先去检索历史案例了。
        # 检索历史案例回答的是「这类事以前怎么处理的」，
        # 它**不回答**「这次是不是这种情况」—— 那个问题只能由检查回答。
        if not scored:
            for name in CONTEXT_TOOLS:
                spec = self.specs.get(name)
                if spec is None or name in called:
                    continue
                if not self._preconditions_met(spec, satisfied):
                    continue
                value = self._context_tool_value(name, hypotheses, active)
                if value > 0:
                    scored.append(ScoredTool(spec, value, [], "上下文工具（兜底）"))

        scored.sort(key=lambda item: (-item.value, PRIORITY.index(item.spec.name)
                                      if item.spec.name in PRIORITY else 99))
        return scored

    # ==================================================================
    def select(
        self,
        hypotheses: dict[str, Hypothesis],
        called: set[str],
        *,
        satisfied: set[str] | None = None,
        cost_penalty: float = 0.12,
    ) -> ScoredTool | None:
        ranked = self.rank(hypotheses, called, satisfied=satisfied,
                           cost_penalty=cost_penalty)
        return ranked[0] if ranked else None

    # ==================================================================
    @staticmethod
    def _preconditions_met(spec: ToolSpec, satisfied: set[str]) -> bool:
        return all(name in satisfied for name in spec.preconditions)

    @staticmethod
    def _context_tool_value(
        name: str, hypotheses: dict[str, Hypothesis], active: dict[str, Hypothesis]
    ) -> float:
        """上下文工具的时机判断。

        * ``search_similar_cases``：**只有在已经有一个假设被支持之后**才值得查。
          在没有明确方向时去检索历史案件，得到的是「一堆看起来都沾边的案例」——
          它会制造噪声而不是信息（§4.4）；
        * ``query_risk_model_detail``：只在第一步用一次，用来理解告警里的模型分
          是怎么来的。它是「读题」而不是「调查」。
        """
        if name == "search_similar_cases":
            supported = [item for item in hypotheses.values()
                         if item.status.value == "SUPPORTED"]
            return 1.0 if supported else 0.0
        if name == "query_risk_model_detail":
            return 0.6 if not active or all(item.weight < 0.9 for item in active.values()) else 0.0
        return 0.0
