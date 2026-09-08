"""反思与蒸馏 (Reflect / Distill)：把长任务轨迹提炼成可沉淀的记忆。

说明书把“反思与蒸馏”列为 8 原语之一(从长任务轨迹中提炼因果规律)，但示例代码块没有展开，
这里补齐其工程形态：

* ``Step``          —— 一次任务动作(action 动词短语 + 是否成功)；
* ``distill_trajectory`` —— 把“步骤序列 + 最终结果”蒸馏成 1~2 条高价值事实：
  成功 → 沉淀“可行流程”EPISODIC；失败 → 沉淀“避坑教训”EPISODIC；
* ``reflect``       —— 更进一步，从反复出现的模式里抽一条 SEMANTIC 因果规律。

真实系统里这里的“蒸馏”是一个 LLM 在异步离线层做因果归因；Demo 用确定性规则模拟，
保证可复现、可测试。生产的替换点就是 ``distill_trajectory`` 的函数体。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..records import EPISODIC, SEMANTIC, MemoryType

SUCCESS_CONFIDENCE = 0.90   # 成功流程：高置信(可复用)
LESSON_CONFIDENCE = 0.70    # 避坑教训：中置信(尚未多次验证)
RULE_CONFIDENCE = 0.60      # 反思规律：低置信起始,靠 Touch 强化


@dataclass
class Step:
    """Agent 任务轨迹中的一个动作。"""

    action: str      # 动词短语，如 "拉取 MME 话统" / "ping 网关"
    ok: bool = True  # 该步是否成功
    detail: str = ""


@dataclass
class DistilledFact:
    """蒸馏产物：可直接喂给 EpisodicMemory.save_fact 的一条记忆。"""

    subject: str
    predicate: str
    object_value: str
    memory_type: MemoryType = EPISODIC
    confidence: float = SUCCESS_CONFIDENCE

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "object_value": self.object_value,
            "memory_type": self.memory_type,
            "confidence": self.confidence,
        }


def distill_trajectory(
    goal: str,
    steps: Iterable[Step],
    outcome: bool = True,
) -> list[DistilledFact]:
    """从一段任务轨迹蒸馏出可落库的事实。

    * 任务整体成功 → 把“哪些步骤走通了”沉淀为 EPISODIC 可行流程(高置信)；
    * 任务整体失败 → 定位第一个失败动作，沉淀为 EPISODIC 避坑教训(中置信)。
    """
    steps = list(steps)
    ok_steps = [s for s in steps if s.ok]
    if outcome and ok_steps:
        path = " → ".join(s.action for s in ok_steps)
        return [
            DistilledFact(
                subject=goal,
                predicate="可行流程",
                object_value=f"{goal}：{path}",
                memory_type=EPISODIC,
                confidence=SUCCESS_CONFIDENCE,
            )
        ]

    first_fail = next((s for s in steps if not s.ok), None)
    if first_fail is not None:
        return [
            DistilledFact(
                subject=goal,
                predicate="避坑教训",
                object_value=(
                    f"{goal}：动作「{first_fail.action}」失败会导致整体失败，"
                    f"下次需替换该动作{('(' + first_fail.detail + ')') if first_fail.detail else ''}"
                ),
                memory_type=EPISODIC,
                confidence=LESSON_CONFIDENCE,
            )
        ]
    return []


def reflect(goal: str, outcome: bool = True) -> Optional[DistilledFact]:
    """反思：从一次经历里抽一条轻量 SEMANTIC 规律(因果归因的最朴素形态)。

    成功时把 goal 本身泛化为“可按此目标标准化作业”；失败时强调“先验证前置条件”。
    生产实现应由 LLM 生成真正跨实例的规律；此处展示“反思→写入语义层”的通道。
    """
    if outcome:
        value = f"目标「{goal}」已有成功先例，同类请求可优先复用既有流程。"
    else:
        value = f"目标「{goal}」此前失败过，执行前应先复核前置条件，避免重蹈覆辙。"
    return DistilledFact(
        subject=goal,
        predicate="反思规律",
        object_value=value,
        memory_type=SEMANTIC,
        confidence=RULE_CONFIDENCE,
    )
