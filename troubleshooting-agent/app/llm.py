"""确定性 LLM 替身 —— 让演示可复现，同时保留「模型会犯错」这件事。

说明书对模型在排障系统里的位置有很明确的定位（§2.1 的职责表）：

    | 排障主控 Agent | 理解诊断状态、选择下一步动作 | 需要 |
    | 诊断规划器     | 根据假设和证据生成检查流程   | 可以使用，但应受约束 |
    | 诊断执行引擎   | 调度工具、管理状态、执行 Workflow | 不需要 |
    | 证据验证器     | 校验实际结果是否支持某个假设 | 优先使用规则和确定性检查 |

所以模型在本 Demo 里出现在**两个**位置，且都被夹在确定性代码中间：

1. 分类兜底（规则命不中时）—— 输出必须过 Schema 校验；
2. 计划提议（模板不适用时）—— 输出只是**候选**，必须过 Workflow Validator。

:class:`ScriptedLLM` 的 ``propose_plan`` 故意提供了几种**非法**输出
（成环、越权、未知 skill、契约不一致、超预算），不是为了演示「模型很蠢」，
而是为了证明校验器真的在拦 —— 一个从不被触发的校验器等于没有。

真实接入时把这个类换成 API 调用即可：它对外只有三个方法，
返回的都是可以 :func:`pydantic.BaseModel.model_validate` 的普通字典。
"""
from __future__ import annotations

from typing import Any

from .domain.enums import FailureCategory, Observation
from .domain.models import FailureContext, Hypothesis, TraceAnalysis


class ScriptedLLM:
    """可预测的模型替身。"""

    def __init__(self, plan_style: str = "valid") -> None:
        self.plan_style = plan_style
        self.calls: list[str] = []

    # ==================================================================
    # 1. 故障分类兜底（§4.2 的「否 → 调用 LLM 分类」分支）
    # ==================================================================
    def classify_failure(
        self,
        ctx: FailureContext,
        trace: TraceAnalysis,
    ) -> dict[str, Any]:
        """给出候选类别。

        这里刻意做成**一个粗糙但合理的猜测**：看到「预期/实际不一致」且配置里有
        SRS 优化开关，就猜配置异常。它经常猜错 —— 而这正是后续证据驱动循环存在的意义：
        分类只是起点，不是判决。说明书 §4.2 也提醒过，``confidence``
        「不应直接视为经过校准的真实概率」。
        """
        self.calls.append("classify_failure")
        has_diff = bool(ctx.expected_actual_diff)
        has_srs_flag = "SrsOptimizationEnabled" in ctx.test_config

        # 连一份可读的材料都没有（日志轮转、配置读不到、环境状态未知）——
        # 这时候提出的「候选类别」是噪声。返回 UNKNOWN 比编一个类别诚实得多，
        # 也正是说明书 §4.2 反复强调的那条边界。
        if not ctx.raw_logs and not ctx.test_config:
            return {
                "primary_category": FailureCategory.UNKNOWN.value,
                "secondary_categories": [],
                "candidate_causes": [],
                "confidence": 0.0,
                "next_checks": ["analyze_logs", "check_environment_state", "inspect_config"],
                "rationale": "日志、配置均不可读，只有一条被截断的错误信息，无法提出候选类别",
            }

        if has_diff and has_srs_flag:
            return {
                "primary_category": FailureCategory.CONFIG.value,
                "secondary_categories": [FailureCategory.TEST_CASE.value],
                "candidate_causes": [
                    "SRS 测量优化相关配置取值与需求约束不一致",
                    "用例预期结果可能未随需求版本更新",
                ],
                "confidence": 0.55,
                "next_checks": ["inspect_config", "validate_case_definition"],
                "rationale": "预期/实际在测量状态上不一致，且启用了 SRS 优化开关",
            }
        if has_diff:
            return {
                "primary_category": FailureCategory.TEST_CASE.value,
                "secondary_categories": [FailureCategory.SOFTWARE.value],
                "candidate_causes": ["预期结果定义可能有问题"],
                "confidence": 0.4,
                "next_checks": ["validate_case_definition"],
                "rationale": "只有预期/实际不一致，缺少其它线索",
            }
        # 连猜都猜不出来 —— 返回 UNKNOWN 比编一个类别诚实（§4.2）
        return {
            "primary_category": FailureCategory.UNKNOWN.value,
            "secondary_categories": [],
            "candidate_causes": [],
            "confidence": 0.0,
            "next_checks": ["analyze_logs", "check_environment_state"],
            "rationale": "证据不足以提出候选类别",
        }

    # ==================================================================
    # 2. 计划提议（§6.3：只输出结构化节点，绝不输出代码）
    # ==================================================================
    def propose_plan(
        self,
        ctx: FailureContext,
        manager: HypothesisManagerLike,
    ) -> dict[str, Any] | None:
        """提议一份候选 DAG。**注意它返回的是数据，不是代码。**

        形状与说明书 §6.3 的例子完全一致：``nodes[].{id, skill, depends_on}``。
        模型能表达的自由度仅此而已 —— 没有 shell、没有表达式、没有循环。
        """
        self.calls.append(f"propose_plan:{self.plan_style}")
        active = [h.key for h in manager.active()]

        if self.plan_style == "valid":
            nodes = [
                {"id": "N1", "skill": "check_environment_state", "depends_on": []},
                {"id": "N2", "skill": "inspect_config", "depends_on": []},
                {
                    "id": "N3",
                    "skill": "retrieve_raw_log",
                    "depends_on": ["N1", "N2"],
                    "inputs": {"from": "N1"},
                },
            ]
            return {
                "mode": "DYNAMIC",
                "nodes": nodes,
                "rationale": f"按当前候选假设 {active} 提出三个互不冲突的检查",
                "proposed_by": "llm",
            }

        if self.plan_style == "cyclic":
            # 模型把「先看日志再定环境」和「先定环境再看日志」写成了互相依赖
            return {
                "mode": "DYNAMIC",
                "nodes": [
                    {"id": "N1", "skill": "analyze_logs", "depends_on": ["N2"]},
                    {"id": "N2", "skill": "check_environment_state", "depends_on": ["N1"]},
                ],
                "rationale": "模型写出了一个环 —— 校验器必须拦下",
            }

        if self.plan_style == "unauthorized":
            # 模型「顺手」把恢复动作写进了诊断计划 —— 这正是 §6.3 第 8 条要拦的
            return {
                "mode": "DYNAMIC",
                "nodes": [
                    {"id": "N1", "skill": "check_environment_state", "depends_on": []},
                    {"id": "N2", "skill": "restart_device", "depends_on": ["N1"]},
                ],
                "rationale": "模型想直接重启设备 —— 需要在 5.1 节被拦下",
            }

        if self.plan_style == "unknown_skill":
            return {
                "mode": "DYNAMIC",
                "nodes": [
                    {"id": "N1", "skill": "shell_exec", "depends_on": [],
                     "inputs": {"cmd": "cat /var/log/srs.log"}},
                ],
                "rationale": "模型凭空发明了一个动作池里不存在的能力",
            }

        if self.plan_style == "schema_mismatch":
            # 消费了 N1 的输出，却没把它写进依赖 —— 输入契约与依赖图不一致
            return {
                "mode": "DYNAMIC",
                "nodes": [
                    {"id": "N1", "skill": "check_environment_state", "depends_on": []},
                    {"id": "N2", "skill": "inspect_tool_records", "depends_on": [],
                     "inputs": {"from": "N1"}},
                ],
                "rationale": "模型漏写了依赖关系",
            }

        if self.plan_style == "resource_conflict":
            # 两个都要求独占 environment 资源的节点，却互不依赖
            return {
                "mode": "DYNAMIC",
                "nodes": [
                    {"id": "N1", "skill": "refresh_environment_state", "depends_on": []},
                    {"id": "N2", "skill": "reallocate_environment", "depends_on": []},
                ],
                "rationale": "模型想同时刷新与重新申请环境",
            }

        if self.plan_style == "budget":
            return {
                "mode": "DYNAMIC",
                "nodes": [
                    {"id": f"N{i}", "skill": "analyze_logs", "depends_on": []}
                    for i in range(1, 10)
                ],
                "rationale": "模型想一次性跑九个检查 —— 超预算",
            }

        return None

    # ==================================================================
    # 3. 结论摘要（只在已经有确定性结论之后调用）
    # ==================================================================
    def summarize(self, conclusion: Any) -> str:
        """把结构化结论写成人话。

        **它只做措辞，不做判断** —— 判断在 :mod:`app.hypothesis.manager`
        的结论门里。把这两件事混在一起，是「让 LLM 决定够不够充分证据」的常见翻车点。
        """
        self.calls.append("summarize")
        state = conclusion.terminal_state.value
        if conclusion.root_cause:
            return f"[{state}] 根因：{conclusion.root_cause}"
        return f"[{state}] 未能在预算内定位根因，建议人工介入"


class HypothesisManagerLike:
    """仅用于类型提示的协议（避免 llm.py 反向依赖 hypothesis 包）。"""

    def active(self) -> list[Hypothesis]:  # pragma: no cover - 协议声明
        raise NotImplementedError


def observation_name(observation: Observation) -> str:
    """观测结果的英文名（供终端展示）。"""
    return observation.value
