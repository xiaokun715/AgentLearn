"""AgentMemorySystem：把四层记忆串成说明书 §记忆层级间的流转与代谢闭环。

一次请求的完整路径(见 README 首图)：

    用户提问
      │ ① 热注水(Hydration)：Profile 点查(Redis,静态) + Episodic Top-K 召回(向量) + Semantic 点查
      ▼
    工作记忆(Working)  ← 由 system.hydrate() 组装帧
      │ ② 会话中：短期记忆(Short-Term) 记录每一轮 + Checkpointer 快照
      ▼
    任务终态
      │ ③ 经验蒸馏：system.commit_session() → worker.distill 提炼 EPISODIC / SEMANTIC 事实落库
      ▼
    consolidate_sweep()：碎片积累足够时合并升阶为 SOP；DecayWorker 负责 ④ 新陈代谢
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..config import AgentMemoryConfig
from ..engine import MemoryEngine
from ..records import EPISODIC, MemoryRecord
from ..worker.distill import DistilledFact, Step, distill_trajectory, reflect
from .episodic import EpisodicMemory
from .semantic import ProfileStore, SemanticMemory
from .shortterm import ShortTermMemory
from .working import WorkingMemory

DEFAULT_USER = "alice"


@dataclass
class CannedScenario:
    """examples/API 可回放的“模拟 Agent 会话”剧本。"""

    user_id: str
    goal: str
    system_prompt: str
    steps: list[Step]
    outcome: bool = True


#: 与“记忆/排障”主题贴合的一个默认剧本，用于 demo_2 / 观测 API 的会话演示
CANNED_5G = CannedScenario(
    user_id=DEFAULT_USER,
    goal="排查 5G 基站闪断",
    system_prompt="你是通信网优工程师 Agent。回答前先回忆历史排障经验。",
    steps=[
        Step("查 MME 话统确认小区负载", ok=True),
        Step("抓取 S1 信令找切换失败点", ok=True),
        Step("核查邻区漏配", ok=True),
        Step("重启该小区观察 30 分钟", ok=True),
    ],
    outcome=True,
)

CANNED_EPISODIC_SEED = [
    # (subject, predicate, object_value) —— 说明书中"上次遇到 XX 如何排查"的同类经验。
    # subject 刻意选取 demo 会话会“原样追问”的话题词，让词袋嵌入能稳定命中。
    ("5G 基站闪断", "排障经验", "先查小区级退服/闪断计数，再看切换失败率与上行干扰，最后核查邻区漏配与射频告警"),
    ("VoLTE 掉话", "排障经验", "优先核查 EPS fallback 后无 4G 邻区导致的掉话，需补邻区"),
    ("上行干扰", "排障经验", "上行干扰多为外部干扰源：关站轮询 + 扫频仪定位后上报处理"),
    ("NR 切换失败", "排障经验", "切换失败先看测量报告与目标小区负载，排除同频干扰再查参数"),
]


class AgentMemorySystem:
    """跨层编排器：热注水 → 会话 → 蒸馏 → 合并。"""

    def __init__(
        self,
        engine: MemoryEngine,
        working: WorkingMemory,
        shortterm: ShortTermMemory,
        episodic: EpisodicMemory,
        profile: ProfileStore,
        semantic: SemanticMemory,
        config: AgentMemoryConfig,
    ) -> None:
        self.engine = engine
        self.working = working
        self.shortterm = shortterm
        self.episodic = episodic
        self.profile = profile
        self.semantic = semantic
        self.config = config

    # ------------------------------------------------------------------
    # ① 热注水 Hydration —— Profile 点查 + Episodic Top-K 召回 + Semantic 点查 → 工作记忆
    # ------------------------------------------------------------------
    def hydrate(
        self,
        user_id: str,
        query: str,
        system_prompt: str = "",
        include_shortterm: bool = True,
    ) -> dict[str, Any]:
        """请求进来时组装“模型真正看到的上下文”。

        分层策略：确定性画像(Profile)全量点查、零检索；经验走语义向量 Top-K 召回；
        命中主题的确定性规则(Semantic)按 subject 点查。这正是说明书
        “确定性配置走点查、经验走向量，杜绝向量近似误召回”的体现。
        """
        self.working.clear()

        # 系统约束帧(钉住，不参与淘汰)
        if system_prompt:
            self.working.add("system", system_prompt, source="system")

        # Profile —— 静态热加载(Redis HGETALL 语义)
        profile_lines = self.profile.as_lines(user_id)
        for line in profile_lines:
            self.working.add("profile", line, source="ProfileStore")

        # Semantic —— 命中主题的确定性规则点查
        rule_hits = self.semantic.point_query_by_text(user_id, query)
        for r in rule_hits:
            self.working.add("semantic", f"[规则] {r.subject}: {r.object_value}", source=f"Semantic/{r.memory_id}")

        # Episodic —— 语义向量 Top-K 召回(few-shot)
        recalled = self.episodic.recall(query, user_id)
        for r, _score in recalled:
            self.working.add(
                "episodic",
                f"回忆：{r.subject} | {r.object_value}",
                source=f"EpisodicRecall/{r.memory_id}",
            )

        # 短期记忆(最近对话/折叠摘要)也作为工作记忆素材(说明书：会话历史折叠注入)
        if include_shortterm:
            for line in self.shortterm.context_lines():
                self.working.add("turn", line, source="ShortTerm")

        context = self.working.assemble_context(system_prompt="")
        return {
            "user_id": user_id,
            "query": query,
            "context": context,
            "profile": profile_lines,
            "recalled": [
                {"memory_id": r.memory_id, "subject": r.subject,
                 "object_value": r.object_value, "confidence": r.confidence, "score": round(s, 4)}
                for r, s in recalled
            ],
            "semantic_hits": [{"subject": r.subject, "object_value": r.object_value} for r in rule_hits],
            "working": self.working.snapshot(),
        }

    # ------------------------------------------------------------------
    # ② 会话记录：短期记忆
    # ------------------------------------------------------------------
    def observe(self, user_id: str, query: str, system_prompt: str = "") -> dict[str, Any]:
        """开始/推进一轮：把用户问题记为短期记忆并热注水。"""
        self.shortterm.append("user", query)
        return self.hydrate(user_id, query, system_prompt)

    # ------------------------------------------------------------------
    # ③ 任务终态：离线蒸馏沉淀记忆
    # ------------------------------------------------------------------
    def commit_session(
        self,
        user_id: str,
        goal: str,
        steps: Iterable[Step] | Iterable[dict[str, Any]],
        outcome: bool = True,
        also_reflect: bool = True,
    ) -> list[MemoryRecord]:
        """会话达成终态后，把轨迹蒸馏成事实并写入记忆(说明书 §经验蒸馏)。

        steps 可以是 ``Step`` 列表，也可以是 {"action","ok","detail"} dict 列表(API 友好)。
        """
        step_objs = [
            s if isinstance(s, Step) else Step(action=s["action"], ok=s.get("ok", True), detail=s.get("detail", ""))
            for s in steps
        ]
        facts = distill_trajectory(goal, step_objs, outcome)
        saved: list[MemoryRecord] = []
        for f in facts:
            saved.append(
                self.episodic.save_fact(
                    user_id=user_id,
                    subject=f.subject,
                    predicate=f.predicate,
                    object_value=f.object_value,
                    memory_type=f.memory_type,
                    confidence=f.confidence,
                )
            )
        if also_reflect and outcome:
            r = reflect(goal, outcome)
            if r is not None:
                saved.append(
                    self.semantic.save_rule(
                        user_id=user_id,
                        subject=r.subject,
                        object_value=r.object_value,
                        predicate=r.predicate,
                        confidence=r.confidence,  # 保留反思初始的低置信度, 靠 Touch 强化
                    )
                )
        self.shortterm.append("assistant", f"完成「{goal}」,已沉淀 {len(facts)} 条经验")
        return saved

    # ------------------------------------------------------------------
    # ④ Consolidate：碎片积累 → 合并升阶 SOP
    # ------------------------------------------------------------------
    def consolidate_sweep(
        self,
        user_id: str,
        subject: str,
        min_fragments: int = 3,
        force: bool = False,
    ) -> MemoryRecord | None:
        """把同一 subject 下积累的碎片情景经验合并成一条 SEMANTIC SOP。

        通用化文案没有 LLM 参与时只能做确定性拼接；生产里这段“概括”交给 LLM 归纳。
        返回新 SOP；碎片不足时返回 None(或 force=True 强制合并)。
        """
        fragments = [
            r
            for r in self.engine.active_memories(user_id)
            if r.memory_type == EPISODIC and r.subject == subject
        ]
        if len(fragments) < min_fragments and not force:
            return None
        counts: dict[str, int] = {}
        for f in fragments:
            key = f.predicate
            counts[key] = counts.get(key, 0) + 1
        summary = "；".join(f"{k}×{v}" for k, v in counts.items())
        generalized = f"{subject}：累计 {len(fragments)} 次事件({summary})，可升阶为标准化 SOP 复用"
        return self.engine.consolidate(
            memory_ids=[f.memory_id for f in fragments],
            generalized_value=generalized,
        )

    # ------------------------------------------------------------------
    # 可回放剧本(供 demo / API 演示端到端闭环)
    # ------------------------------------------------------------------
    def run_canned_session(self, scenario: CannedScenario | None = None) -> list[dict[str, Any]]:
        """按固定剧本跑一遍「热注水 → 蒸馏落库 → 再热注水命中」并返回每步展示数据。"""
        sc = scenario or CANNED_5G
        stages: list[dict[str, Any]] = []

        # 第 1 幕：任务开始,先看模型能回忆到什么
        hydration = self.hydrate(sc.user_id, sc.goal, sc.system_prompt)
        stages.append({"title": "新任务到达 · 热注水", "data": {
            "goal": sc.goal,
            "recalled_count": len(hydration["recalled"]),
            "recalled": hydration["recalled"],
            "context": hydration["context"],
        }})

        # 第 2 幕：任务执行完 → 离线蒸馏写入记忆
        saved = self.commit_session(sc.user_id, sc.goal, sc.steps, outcome=sc.outcome)
        stages.append({"title": "任务终态 · 蒸馏沉淀", "data": {
            "saved": [r.to_dict() for r in saved],
        }})

        # 第 3 幕：下一次同类任务 → 能命中刚沉淀的新经验(few-shot 升级)
        next_query = f"另一个{sc.goal[:4]}的站点也报障,怎么处理?"
        hydration2 = self.hydrate(sc.user_id, next_query, sc.system_prompt)
        stages.append({"title": "下一次同类请求 · 命中新经验", "data": {
            "query": next_query,
            "recalled": hydration2["recalled"],
            "context": hydration2["context"],
        }})
        return stages

    # ------------------------------------------------------------------
    # 观测快照
    # ------------------------------------------------------------------
    def tiers_snapshot(self, user_id: str = DEFAULT_USER) -> dict[str, Any]:
        """GET /v1/tiers 的四层总览。"""
        return {
            "user_id": user_id,
            "working": self.working.snapshot(),
            "shortterm": self.shortterm.snapshot(),
            "episodic_counts": self.episodic.snapshot(user_id),
            "profile": self.profile.get(user_id),
            "semantic_rules": [
                {"memory_id": r.memory_id, "subject": r.subject,
                 "object_value": r.object_value, "confidence": r.confidence}
                for r in self.semantic.all_rules(user_id)
            ],
            "engine_stats": self.engine.stats(),
        }
