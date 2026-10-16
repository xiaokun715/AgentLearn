"""Workflow 生成（说明书 §8）。

两条路径，**确定性规划器为主、LLM 提议为备**：

    CapabilityPlanner   意图 -> 需要的「能力 -> Agent」映射 -> 依赖排序 -> DAG
    LLM 提议             模型给一份 steps[] -> 走同一个校验器

为什么把确定性规划器放在主路径（§8.1）：**同一个意图应该生成同一份 DAG**。
如果每次都让模型自由发挥，那么「这个意图的最佳编排」永远无法被沉淀 ——
每次生成的形状都不同，模板库里会攒下一堆同义但不同的模板，
§12.2 的结构去重也就永远命不中。

LLM 的价值在于**覆盖面**（它能提出人没预料到的步骤组合），
而不是**稳定性**。所以它是补充，且必须过校验。

## 数据流闭合（§8.3）—— 最容易漏的一条

一个 DAG 无环、每个 Agent 都存在，**依然可能是不可执行的**：
某一步 ``needs: [env_state]``，但前面没有任何步骤产出 ``env_state``。
这类错误在「按 agent 名字连一条链」的写法里极常见，而且只有在真跑起来
才会暴露。本模块用 ``produces`` / ``needs`` 把数据流显式建出来，
并在生成阶段就把它闭合。
"""
from __future__ import annotations

from ..domain.enums import RiskLevel, TemplateSource
from ..domain.models import (
    Intent,
    TemplateTags,
    WorkflowStep,
    WorkflowTemplate,
)
from ..pool.agents import AgentPool

#: 编排本身的固定成本：一次 LLM 往返 + 一次校验。
#: 它只应该在「第一次」发生时出现 —— 这也是模板复用在延迟上的全部收益。
COMPOSE_LLM_LATENCY_MS = 600
COMPOSE_VALIDATE_LATENCY_MS = 40


class ComposeError(Exception):
    """生成失败：不是「模型没想出来」，而是**这个意图在当前池子里无法被完成**。"""


class CapabilityPlanner:
    """确定性的能力规划器。"""

    def __init__(self, pool: AgentPool) -> None:
        self.pool = pool
        self.notes: list[str] = []

    # ==================================================================
    def compose(self, intent: Intent, *, provided_keys: list[str] | None = None) -> WorkflowTemplate:
        """把意图编译成一份可执行的 DAG。"""
        self.notes = []
        provided = set(provided_keys or [])
        wanted = list(intent.required_capabilities)

        # ---- ① 能力 -> Agent -----------------------------------------
        chosen: list[str] = []
        missing_caps: list[str] = []
        for capability in wanted:
            candidates = self.pool.by_capability(capability)
            if not candidates:
                missing_caps.append(capability)
                continue
            # 选最便宜的可用 Agent；同价按名字排，保证确定性
            best = min(candidates, key=lambda item: (item.cost, item.latency_ms, item.name))
            if best.name not in chosen:
                chosen.append(best.name)
                self.notes.append(f"能力 {capability} -> {best.name}")

        if missing_caps:
            raise ComposeError(f"池子里没有任何 Agent 能提供这些能力：{sorted(missing_caps)}")

        # ---- ② 补齐中间的数据生产者（数据流闭合，§8.3）----------------
        chosen, extra = self._satisfy_reads(chosen, provided)
        if extra:
            self.notes.append(f"为闭合数据流，额外插入：{extra}")

        # ---- ③ 依赖排序 ----------------------------------------------
        steps = self._order(chosen, provided)
        self.notes.append(f"排成 {len(steps)} 步的 DAG")

        return WorkflowTemplate(
            name=f"（现场生成）{intent.name}",
            description="；".join(self.notes[-3:]),
            steps=steps,
            tags=TemplateTags(
                author="",                       # 由调用方填入发起人（需求 4）
                source=TemplateSource.LLM_GENERATED,
                domain=self.pool.require(chosen[0]).domain if chosen else "general",
                intent=intent.name,
                risk=self._max_risk(chosen),
                labels=[],
                origin_query=intent.raw_query,
            ),
        )

    # ==================================================================
    def _satisfy_reads(self, chosen: list[str], provided: set[str]) -> tuple[list[str], list[str]]:
        """确保每个被选 Agent 要读的键，都有人产出。"""
        available = set(provided)
        inserted: list[str] = []
        frontier = list(chosen)
        guard = 0

        while guard < 32:
            guard += 1
            produced = set()
            for name in chosen:
                produced |= set(self.pool.require(name).writes)
            available_now = available | produced

            unsatisfied: list[str] = []
            for name in chosen:
                for key in self.pool.require(name).reads:
                    if key not in available_now:
                        unsatisfied.append(key)
            if not unsatisfied:
                break

            # 找一个能产出缺失键的 Agent 补进去
            key = unsatisfied[0]
            producers = self.pool.agents_producing(key)
            if not producers:
                raise ComposeError(
                    f"数据流无法闭合：没有任何 Agent 能产出 {key!r}"
                    f"（被 {[n for n in chosen if key in self.pool.require(n).reads]} 需要）"
                )
            best = min(producers, key=lambda item: (item.cost, item.name))
            if best.name in chosen:
                # 已经在链上却还缺 —— 说明排序解决不了，直接报错而不是死循环
                raise ComposeError(f"数据流无法闭合：{key!r} 的产出者已在链上但顺序无解")
            chosen.insert(0, best.name)
            inserted.append(best.name)
            frontier.append(best.name)

        if guard >= 32:
            raise ComposeError("数据流闭合时超出最大迭代次数（疑似循环依赖）")

        # 反向检查：被插进来的 Agent 自己有没有没人满足的 reads
        for name in inserted:
            for key in self.pool.require(name).reads:
                producers = [n for n in chosen if key in self.pool.require(n).writes]
                if not producers and key not in provided:
                    raise ComposeError(f"插入的 {name} 需要 {key!r}，但链上没人产出它")
        return chosen, inserted

    # ==================================================================
    def _order(self, agents: list[str], provided: set[str]) -> list[WorkflowStep]:
        """按数据依赖做拓扑排序，产出步骤列表。"""
        remaining = list(agents)
        steps: list[WorkflowStep] = []
        produced_by: dict[str, str] = {}     # 黑板键 -> 产出它的 step id
        available = set(provided)

        index = 0
        while remaining:
            progressed = False
            for name in list(remaining):
                spec = self.pool.require(name)
                if all(key in available for key in spec.reads):
                    index += 1
                    step_id = f"g{index}"
                    depends = sorted({
                        produced_by[key] for key in spec.reads if key in produced_by
                    })
                    steps.append(WorkflowStep(
                        id=step_id,
                        agent=name,
                        depends_on=depends,
                        needs=list(spec.reads),
                        produces=list(spec.writes),
                        note=spec.description,
                    ))
                    for key in spec.writes:
                        produced_by[key] = step_id
                        available.add(key)
                    remaining.remove(name)
                    progressed = True
            if not progressed:
                # 剩下的互为依赖 —— 数据流有环，这在生成阶段就该失败
                blocked = {name: self.pool.require(name).reads for name in remaining}
                raise ComposeError(f"依赖无法排序（疑似循环）：{blocked}")
        return steps

    def _max_risk(self, agents: list[str]) -> RiskLevel:
        levels = [self.pool.require(name).risk for name in agents] or [RiskLevel.LOW]
        return max(levels, key=lambda level: level.order)


# ======================================================================
# LLM 提议路径
# ======================================================================
class LLMComposer:
    """把模型的自由发挥收进一个受约束的形状里。"""

    def __init__(self, pool: AgentPool, llm) -> None:
        self.pool = pool
        self.llm = llm

    def propose(self, intent: Intent) -> tuple[WorkflowTemplate | None, list[str]]:
        """返回 ``(模板, 备注)``。模型提议失败时返回 ``None`` 让调用方回退。"""
        if self.llm is None:
            return None, ["没有可用的 LLM，跳过提议路径"]
        raw = self.llm.compose_workflow(intent, self.pool.snapshot())
        steps: list[WorkflowStep] = []
        notes: list[str] = []
        for index, item in enumerate(raw.get("steps", []), start=1):
            agent = item.get("agent", "")
            if self.pool.get(agent) is None:
                notes.append(f"模型提议的 {agent} 不在池子里，已丢弃该步骤")
                continue
            steps.append(WorkflowStep(
                id=item.get("id") or f"m{index}",
                agent=agent,
                depends_on=list(item.get("depends_on", [])),
                needs=list(item.get("needs", [])),
                produces=list(item.get("produces", [])),
            ))
        if not steps:
            return None, notes + ["模型没有给出任何可用步骤"]
        template = WorkflowTemplate(
            name=f"（模型提议）{intent.name}",
            description=raw.get("description", ""),
            steps=steps,
            tags=TemplateTags(
                author="",
                source=TemplateSource.LLM_GENERATED,
                domain="general",
                intent=intent.name,
                risk=RiskLevel.LOW,
                origin_query=intent.raw_query,
            ),
        )
        return template, notes
