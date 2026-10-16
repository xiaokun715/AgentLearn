"""Agent Pool —— 池子里的**已生成 Workflow 模板**（说明书 §11 / §12 / §13）。

这是需求 1 的另一半，也是本 Demo 与「一次性的 DAG 生成器」最大的差别：

    一次性生成器：query → 生成 DAG → 跑完丢掉
    本 Demo    ：query → 生成 DAG → 跑完 → **带标签入库** → 下次直接命中

模板库的价值不在「省了一次生成」，而在**它把一次偶然的成功变成了可复用的资产**。
而要让这件事成立，模板必须能被治理 —— 治理的前提是标签（§11.4）。

这个类负责四件事：
1. 入库时的质量门（§12.1）；
2. 去重（结构指纹，§12.2）；
3. 检索（按意图 / 标签 / 关键词，§6.4）；
4. 生命周期（失效检测、版本演进、淘汰，§13）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..domain.enums import RiskLevel, TemplateSource, TemplateState
from ..domain.models import (
    TemplateStats,
    TemplateTags,
    WorkflowStep,
    WorkflowTemplate,
)
from .agents import AgentPool


@dataclass
class SaveOutcome:
    """一次入库尝试的结果。``stored=False`` 时 ``reason`` 说明为什么没存。"""

    template: WorkflowTemplate
    stored: bool
    decision: str
    reason: str = ""
    merged_into: str | None = None


@dataclass
class LibraryReport:
    total: int = 0
    by_state: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    by_domain: dict[str, int] = field(default_factory=dict)
    broken: list[str] = field(default_factory=list)
    duplicates_merged: int = 0


class TemplateStore:
    """带标签的模板库。"""

    def __init__(self, pool: AgentPool, *, max_templates: int = 200) -> None:
        self.pool = pool
        self.max_templates = max_templates
        self._templates: dict[str, WorkflowTemplate] = {}
        #: 结构指纹 -> template_id，去重索引
        self._by_structure: dict[str, str] = {}
        self.audit: list[dict] = []

    # ==================================================================
    # 写入（§12）
    # ==================================================================
    def add(self, template: WorkflowTemplate, *, require_verified: bool = True) -> SaveOutcome:
        """入库。这里就是 §12.1 的**质量门**。

        默认要求「先跑过一次且成功」才允许入库。理由是本书反复出现的那条：
        **不要让生成者自己决定成果是否够格**。一份刚生成、从没执行过的 DAG，
        它的形状看起来再漂亮，也没有任何证据说明它是对的。
        """
        # ---- 门 1：标签必须齐备 --------------------------------------
        if not template.tags.author:
            return SaveOutcome(template, False, "REJECT_NO_AUTHOR",
                               "缺少生成人标签 —— 模板库里每条记录都必须能追溯到人/进程")
        if not template.tags.intent:
            return SaveOutcome(template, False, "REJECT_NO_INTENT",
                               "缺少意图标签 —— 没有它，这条模板永远不会被检索到")

        # ---- 门 2：必须是被验证过的 ----------------------------------
        if require_verified and template.stats.successes == 0:
            return SaveOutcome(
                template, False, "REJECT_UNVERIFIED",
                "这份模板还没有成功执行过 —— 未经运行验证的 DAG 不允许入库",
            )

        # ---- 门 3：依赖的 Agent 必须还在 -----------------------------
        missing = [step.agent for step in template.steps if self.pool.get(step.agent) is None]
        if missing:
            template.tags.state = TemplateState.BROKEN
            return SaveOutcome(template, False, "REJECT_BROKEN",
                               f"引用了池子里不存在的 Agent：{sorted(set(missing))}")

        # ---- 门 4：结构去重（§12.2）----------------------------------
        fingerprint = template.structure_hash
        existing_id = self._by_structure.get(fingerprint)
        if existing_id is not None and existing_id != template.template_id:
            existing = self._templates[existing_id]
            # 不是简单的「丢弃」：把新模板的信息**合并**进已有模板。
            # 同一份 DAG 被两个人分别生成，说明它是个高频需求 —— 这个信号
            # 应该体现在已有模板的标签上，而不是被丢掉。
            self._merge(existing, template)
            self.audit.append({"action": "MERGE", "into": existing_id,
                               "from": template.template_id})
            return SaveOutcome(template, False, "REJECT_DUPLICATE",
                               f"与已有模板 {existing_id} 结构相同，已合并标签",
                               merged_into=existing_id)

        # ---- 门 5：容量 ----------------------------------------------
        if len(self._templates) >= self.max_templates:
            victim = self._eviction_candidate()
            if victim is not None:
                self._templates.pop(victim.template_id, None)
                self._by_structure.pop(victim.structure_hash, None)
                self.audit.append({"action": "EVICT", "template": victim.template_id})

        # ---- 通过 -----------------------------------------------------
        if template.tags.state is TemplateState.DRAFT:
            template.tags.state = TemplateState.ACTIVE
        self._templates[template.template_id] = template
        self._by_structure[fingerprint] = template.template_id
        self.audit.append({"action": "STORE", "template": template.template_id,
                           "source": template.tags.source.value})
        return SaveOutcome(template, True, "STORED")

    def _merge(self, existing: WorkflowTemplate, incoming: WorkflowTemplate) -> None:
        """把新模板的标签并进已有模板。"""
        for label in incoming.tags.labels:
            if label not in existing.tags.labels:
                existing.tags.labels.append(label)
        if incoming.tags.author not in existing.tags.author:
            existing.tags.author = f"{existing.tags.author},{incoming.tags.author}"
        existing.tags.origin_query = existing.tags.origin_query or incoming.tags.origin_query

    def _eviction_candidate(self) -> WorkflowTemplate | None:
        """§13.2 淘汰谁：从「没人用 + 成功率低 + 不是种子」里挑最差的。

        刻意排除 ``SEED``：平台预置的模板是冷启动的兜底，被自动淘汰掉
        会让系统退回「什么都不会」的状态。
        """
        candidates = [
            template for template in self._templates.values()
            if template.tags.source is not TemplateSource.SEED
            and template.tags.state is not TemplateState.BROKEN
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (item.stats.runs, -item.stats.success_rate, item.tags.created_at),
        )

    # ==================================================================
    # 检索（§6.4）
    # ==================================================================
    def get(self, template_id: str) -> WorkflowTemplate | None:
        return self._templates.get(template_id)

    def all(self) -> list[WorkflowTemplate]:
        return sorted(self._templates.values(), key=lambda item: item.template_id)

    def actives(self) -> list[WorkflowTemplate]:
        return [item for item in self.all() if item.tags.state is TemplateState.ACTIVE]

    def by_intent(self, intent_name: str) -> list[WorkflowTemplate]:
        """按意图检索 —— 路由引擎的主要入口。"""
        return [
            item for item in self.actives()
            if item.tags.intent == intent_name and self.is_executable(item)
        ]

    def by_labels(self, labels: list[str]) -> list[WorkflowTemplate]:
        return [item for item in self.actives() if item.tags.matches_labels(labels)]

    def by_author(self, author: str) -> list[WorkflowTemplate]:
        return [item for item in self.all() if author in item.tags.author]

    def search(self, keyword: str) -> list[WorkflowTemplate]:
        """按名称 / 描述 / 标签做关键词检索（演示用的最小实现）。"""
        needle = keyword.lower()
        results = []
        for item in self.all():
            haystack = " ".join([
                item.name, item.description, item.tags.intent, item.tags.domain,
                *item.tags.labels,
            ]).lower()
            if needle in haystack:
                results.append(item)
        return results

    # ==================================================================
    # 生命周期（§13）
    # ==================================================================
    def is_executable(self, template: WorkflowTemplate) -> bool:
        """模板能不能跑：状态正常 + 引用的 Agent 都还在且健康。"""
        if template.tags.state in (TemplateState.BROKEN, TemplateState.DEPRECATED):
            return False
        return all(
            self.pool.get(step.agent) is not None and self.pool.require(step.agent).usable
            for step in template.steps
        )

    def audit_health(self) -> LibraryReport:
        """§13.3 失效检测：池子变了，模板库必须跟着变。

        这在真实系统里是模板库最大的一个坑：**子 Agent 下线了，
        模板却还引用着它**。等到有人真的命中这条模板时才报错，
        而那时它已经在生产上跑了一半。
        """
        report = LibraryReport(total=len(self._templates))
        for template in self.all():
            if not self.is_executable(template) and template.tags.state is TemplateState.ACTIVE:
                template.tags.state = TemplateState.BROKEN
                report.broken.append(template.template_id)
                self.audit.append({"action": "MARK_BROKEN",
                                   "template": template.template_id})
            report.by_state[template.tags.state.value] = (
                report.by_state.get(template.tags.state.value, 0) + 1
            )
            report.by_source[template.tags.source.value] = (
                report.by_source.get(template.tags.source.value, 0) + 1
            )
            report.by_domain[template.tags.domain] = (
                report.by_domain.get(template.tags.domain, 0) + 1
            )
        return report

    def deprecate(self, old_id: str, new_id: str) -> bool:
        """§12.3 版本演进：旧版保留但不再首选，并指向新版。"""
        old = self._templates.get(old_id)
        new = self._templates.get(new_id)
        if old is None or new is None:
            return False
        old.tags.state = TemplateState.DEPRECATED
        old.tags.superseded_by = new_id
        new.version = old.version + 1
        new.based_on = old_id
        self.audit.append({"action": "DEPRECATE", "old": old_id, "new": new_id})
        return True

    def record_run(self, template_id: str, ok: bool, latency_ms: int) -> None:
        template = self._templates.get(template_id)
        if template is not None:
            template.stats.record(ok, latency_ms)

    # ------------------------------------------------------------------
    def snapshot(self) -> list[dict]:
        return [
            {
                "template_id": item.template_id,
                "name": item.name,
                "intent": item.tags.intent,
                "domain": item.tags.domain,
                "author": item.tags.author,
                "source": item.tags.source.value,
                "state": item.tags.state.value,
                "version": item.version,
                "created_at": item.tags.created_at,
                "labels": list(item.tags.labels),
                "steps": len(item.steps),
                "runs": item.stats.runs,
                "success_rate": round(item.stats.success_rate, 3),
                "avg_latency_ms": item.stats.avg_latency_ms,
            }
            for item in self.all()
        ]


# ======================================================================
# §13.1 冷启动：平台预置模板
# ======================================================================
def _seeded_at(days_ago: int) -> datetime:
    return datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=days_ago)


def seed_templates(pool: AgentPool) -> TemplateStore:
    """平台预置三条模板 —— 让系统在「还没有人用过」的时候也能工作。

    冷启动期只有两种选择：预置模板，或者每次都现场生成。
    后者会让前 N 次请求全都付出编排成本，而且质量全凭模型当时的运气。
    """
    store = TemplateStore(pool)
    seeds = [
        WorkflowTemplate(
            name="测试失败标准排障流程",
            description="采集日志 → 解析 → 环境体检 → 配置比对 → 根因 → 报告",
            steps=[
                WorkflowStep(id="s1", agent="log_collector", produces=["logs"],
                             note="最先做：一切证据的入口"),
                WorkflowStep(id="s2", agent="log_analyzer", depends_on=["s1"],
                             needs=["logs"], produces=["error_events"]),
                WorkflowStep(id="s3", agent="env_prober", produces=["env_state"],
                             note="与环境体检互不依赖，可并行"),
                WorkflowStep(id="s4", agent="config_inspector", produces=["config_diff"],
                             note="与上一步并行"),
                WorkflowStep(id="s5", agent="root_cause_analyst",
                             depends_on=["s2", "s3", "s4"],
                             needs=["error_events", "env_state", "config_diff"],
                             produces=["hypotheses"],
                             note="汇总节点：必须等三条证据到齐"),
                WorkflowStep(id="s6", agent="report_writer", depends_on=["s5"],
                             needs=["hypotheses"], produces=["report"]),
            ],
            tags=TemplateTags(
                author="platform",
                created_at=_seeded_at(30),
                source=TemplateSource.SEED,
                domain="troubleshooting",
                intent="troubleshoot_failure",
                risk=RiskLevel.LOW,
                labels=["排障", "5G", "标准流程"],
                state=TemplateState.ACTIVE,
                origin_query="（平台预置）",
            ),
            stats=TemplateStats(runs=42, successes=39, failures=3,
                                total_latency_ms=42 * 6_150, last_used_at=_seeded_at(1)),
        ),
        WorkflowTemplate(
            name="代码安全审查",
            description="读代码 → 安全检查 → 风格检查",
            steps=[
                WorkflowStep(id="c1", agent="code_reader", produces=["code"]),
                WorkflowStep(id="c2", agent="security_reviewer", depends_on=["c1"],
                             needs=["code"], produces=["security_findings"]),
                WorkflowStep(id="c3", agent="style_reviewer", depends_on=["c1"],
                             needs=["code"], produces=["style_findings"],
                             optional=True, note="风格问题不阻断结论"),
            ],
            tags=TemplateTags(
                author="platform",
                created_at=_seeded_at(21),
                source=TemplateSource.SEED,
                domain="code_review",
                intent="code_review",
                risk=RiskLevel.LOW,
                labels=["代码审查", "安全"],
                state=TemplateState.ACTIVE,
                origin_query="（平台预置）",
            ),
            stats=TemplateStats(runs=18, successes=18, failures=0,
                                total_latency_ms=18 * 1_500, last_used_at=_seeded_at(2)),
        ),
    ]
    for template in seeds:
        store.add(template, require_verified=False)
    return store
