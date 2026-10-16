"""确定性 LLM 替身。

模型在本 Demo 里出现在**两个**位置，且都被夹在确定性代码中间：

1. 意图识别的兜底（规则命不中时）—— 输出必须过 Schema 校验；
2. Workflow 的提议（现场编排时）—— 输出只是**候选**，必须过 Validator。

:class:`ScriptedLLM` 的 ``compose_workflow`` 提供了几种**有缺陷**的提议。
这不是为了演示「模型很蠢」，而是为了让校验器真的被触发 ——
一个从来没拦下过任何东西的校验器，等于没有。
"""
from __future__ import annotations

from typing import Any

from .domain.enums import RiskLevel


class ScriptedLLM:
    """可预测的模型替身。``plan_style`` 控制它提议的编排长什么样。"""

    def __init__(self, plan_style: str = "valid") -> None:
        self.plan_style = plan_style
        self.calls: list[str] = []

    # ==================================================================
    # 1. 意图兜底
    # ==================================================================
    def classify_intent(self, query: str) -> dict[str, Any]:
        """规则没命中时的兜底。

        刻意做成一个**粗糙的**猜测：看到「看看」「处理一下」这类模糊词，
        它会给出一个低置信度的候选，而不是硬编一个意图。
        这正好让路由引擎走进澄清分支 —— 那是它该去的地方。
        """
        self.calls.append("classify_intent")
        lowered = query.lower()
        if any(word in lowered for word in ("看看", "处理", "弄一下", "搞一下", "有问题")):
            return {
                "name": "troubleshoot_failure",
                "confidence": 0.35,
                "required_capabilities": [
                    "log_read", "log_analysis", "env_check", "config_check",
                    "root_cause", "report",
                ],
                "estimated_steps": 6,
                "entities": {},
                "rationale": "query 里出现了「有问题」这类模糊表述，猜测是排障，但证据不足",
            }
        return {
            "name": "unknown",
            "confidence": 0.0,
            "required_capabilities": [],
            "estimated_steps": 1,
            "entities": {},
            "rationale": "没有足够线索判断用户想做什么",
        }

    # ==================================================================
    # 2. Workflow 提议
    # ==================================================================
    def compose_workflow(self, intent, pool_snapshot: list[dict]) -> dict[str, Any]:
        """提议一份步骤列表。**只输出结构，不输出代码。**

        形状与说明书 §8.2 的例子一致：``steps[].{id, agent, needs, produces}``。
        模型能表达的自由度仅此而已。
        """
        self.calls.append(f"compose_workflow:{self.plan_style}")
        caps = list(intent.required_capabilities)
        by_cap = {cap: name for spec in pool_snapshot for cap in spec["capabilities"]
                  for name in [spec["name"]]}
        # 每个能力挑第一个可用 Agent（这只是模型视角的「看起来合理」，
        # 真正的选择与排序由确定性规划器负责）
        steps = []
        previous: str | None = None
        for index, cap in enumerate(caps, start=1):
            agent = by_cap.get(cap)
            if agent is None:
                continue
            spec = next(item for item in pool_snapshot if item["name"] == agent)
            step = {
                "id": f"m{index}",
                "agent": agent,
                "needs": list(spec["reads"]),
                "produces": list(spec["writes"]),
            }
            if previous is not None and spec["reads"]:
                step["depends_on"] = [previous]
            steps.append(step)
            previous = step["id"]

        if self.plan_style == "valid":
            return {"steps": steps, "description": f"按能力顺序串成一条链（{len(steps)} 步）"}

        if self.plan_style == "missing_dependency":
            # 模型忘了写依赖：数据流会断
            for step in steps:
                step.pop("depends_on", None)
            return {"steps": steps,
                    "description": "模型漏写了全部依赖关系（数据流会断）"}

        if self.plan_style == "cyclic":
            if len(steps) >= 2:
                steps[0]["depends_on"] = [steps[-1]["id"]]
                steps[-1]["depends_on"] = [steps[0]["id"]]
            return {"steps": steps, "description": "模型写出了环"}

        if self.plan_style == "phantom_agent":
            if steps:
                steps.insert(0, {"id": "mx", "agent": "universal_solver",
                                 "needs": [], "produces": ["magic"]})
            return {"steps": steps, "description": "模型发明了一个池子里不存在的 Agent"}

        if self.plan_style == "side_effect":
            if steps:
                steps.append({"id": "mz", "agent": "config_applier",
                              "needs": ["config_diff"], "produces": ["config_applied"]})
            return {"steps": steps,
                    "description": "模型「顺手」加了一个会写环境的步骤"}

        if self.plan_style == "over_budget":
            filler = [{"id": f"f{i}", "agent": "knowledge_searcher",
                       "needs": [], "produces": ["references"]} for i in range(12)]
            return {"steps": steps + filler,
                    "description": "模型一口气排了十几个步骤"}

        return {"steps": steps, "description": "(no plan)"}

    # ==================================================================
    # 3. 澄清提问
    # ==================================================================
    def clarification_question(self, query: str, candidates: list[str]) -> str:
        self.calls.append("clarification_question")
        if candidates:
            return f"你是想「{candidates[0]}」，还是「{candidates[1]}」？"
        return "能再具体一点吗？我不确定你想让我做什么。"


def risk_from_name(name: str) -> RiskLevel:
    return RiskLevel.HIGH if name in {"config_apply"} else RiskLevel.LOW
