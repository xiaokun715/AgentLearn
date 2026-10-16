"""确定性 LLM 替身。

模型在本 Demo 里出现的次数**比读者想象的少**，这是刻意的：

| 环节 | 是否需要模型 | 本 Demo 的落地 |
|---|---|---|
| 在线决策（特征/规则/打分/处置）| **不需要** | 纯确定性代码，毫秒级 |
| 调查路径选择 | **不需要** | 规划器按假设权重算，可解释可回归 |
| 证据 → 假设的更新 | **不需要** | 查表规则（ANOMALY 支持 / NORMAL 反驳）|
| 结论判定 | **不需要** | `ConclusionPolicy` 的确定性判据 |
| 把证据归纳成人能读的报告 | 需要 | :meth:`ScriptedLLM.summarize_report` |
| 提出新的风险模式（方式二）| 需要 | :meth:`ScriptedLLM.propose_pattern` |

这个分布本身就是本章的一个论点：**Agent 的价值不等于「到处都用大模型」**。
真正需要模型的是**开放式的归纳**（这段证据说明什么）和**跨域的联想**
（这几条关系可能对应什么攻击模式）—— 不是权限判断，也不是路径选择。

``propose_pattern`` 提供了几种**有缺陷**的提议，用来验证「Agent 提的假设
必须先过离线回放」这条约束真的在起作用。
"""
from __future__ import annotations

from typing import Any


class ScriptedLLM:
    """可预测的模型替身。"""

    def __init__(self, style: str = "valid") -> None:
        self.style = style
        self.calls: list[str] = []

    # ==================================================================
    # 1. 归纳报告（模型真正该做的事）
    # ==================================================================
    def summarize_report(self, *, entity_id: str, path: list[str],
                         supported: list[str], refuted: list[str],
                         missing: list[str]) -> str:
        self.calls.append("summarize_report")
        parts = [f"对 {entity_id} 的调查覆盖了 {len(path)} 个数据源（{' → '.join(path)}）。"]
        if supported:
            parts.append(f"支持的风险假设：{'、'.join(supported)}。")
        if refuted:
            parts.append(f"已被证据反驳：{'、'.join(refuted)}。")
        if missing:
            parts.append(f"仍未有效检验：{'、'.join(missing)}。")
        parts.append("结论仅基于上述已收集的证据，处置需经人工或受控系统执行。")
        return "".join(parts)

    # ==================================================================
    # 2. 提出风险模式（方式二 —— 开放式联想）
    # ==================================================================
    def propose_pattern(self, signals: list[dict]) -> dict[str, Any]:
        """把统计信号联想成一个「可能是什么攻击模式」。

        **它只提出「这像什么」，不提出「这是不是真的」。**
        后者由离线回放回答，前者是模型擅长的部分。
        """
        self.calls.append(f"propose_pattern:{self.style}")

        if self.style == "phantom_signal":
            return {
                "name": "PHANTOM_SIGNAL",
                "description": "基于一个不存在的字段编造的模式",
                "conditions": ["nonexistent_feature > 0"],
                "confidence": 0.9,
                "rationale": "模型自信地引用了一个不存在的数据字段",
            }
        if self.style == "overfit_single":
            return {
                "name": "SINGLE_SIGNAL_OVERFIT",
                "description": "只用单一弱信号（型号字符串）就下的结论",
                "conditions": ["device_model_is"],
                "confidence": 0.85,
                "rationale": "模型抓住一个显著但区分力弱的信号就下结论",
            }
        if self.style == "label_leak":
            return {
                "name": "LABEL_LEAK",
                "description": "使用了标注结果来「定义」模式",
                "conditions": ["user_is_labelled_bad"],
                "confidence": 0.99,
                "rationale": "模型直接引用了标注标签 —— 这在真实场景里根本拿不到",
            }

        return {
            "name": "DISPERSED_FARM_PATTERN",
            "description": (
                "设备型号字符串不可用的设备 + 注册时间高度聚集，"
                "两个弱信号叠加后指向无人值守的设备农场"
            ),
            "conditions": ["device_model_is", "registered_in_burst"],
            "confidence": 0.62,
            "rationale": (
                "单看任一个信号都会大量误伤（该型号有 125 个正常账号），"
                "但两者同时命中的集合只有 35 个 —— 组合之后才具备区分力"
            ),
        }

    # ==================================================================
    # 3. 生成处置说明
    # ==================================================================
    def disposition_note(self, disposition: str, outcome: str) -> str:
        self.calls.append("disposition_note")
        return (
            f"建议处置：{disposition}（依据：{outcome}）。"
            "该建议需要由具备处置权限的人员或系统确认后执行。"
        )
