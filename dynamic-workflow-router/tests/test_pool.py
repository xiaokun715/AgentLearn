"""池子与模板库的单元测试（说明书 §4 / §11 / §12 / §13）。"""
from __future__ import annotations

import pytest

from app.domain.enums import AgentHealth, RiskLevel, TemplateSource, TemplateState
from app.domain.models import (
    TemplateStats,
    TemplateTags,
    WorkflowStep,
    WorkflowTemplate,
)
from app.pool.agents import BUILTIN_AGENTS, AgentPool
from app.pool.templates import TemplateStore


# ======================================================================
# §4 子 Agent
# ======================================================================
def test_builtin_specs_are_not_shared_between_instances():
    """默认值必须每次取一份新的，不能共享一份。

    踩过的坑：直接把 ``BUILTIN_AGENTS`` 里的对象 register 进池子，
    一个实例的 ``set_health`` 会改到所有实例头上 —— 表现是
    「单独跑某个测试能过，一起跑就挂」，而且挂的位置离原因很远。
    """
    first = AgentPool()
    second = AgentPool()
    first.set_health("log_collector", AgentHealth.OFFLINE)
    assert second.require("log_collector").health is AgentHealth.HEALTHY
    assert BUILTIN_AGENTS[0].health is AgentHealth.HEALTHY


def test_covering_ranks_by_coverage_then_cost():
    pool = AgentPool()
    scored = pool.covering(["code_read", "security_review"])
    assert scored
    assert scored[0][0].name == "code_reader"      # 覆盖 1/2，成本最低
    assert scored[0][1] == pytest.approx(0.5)


def test_offline_agents_are_excluded_from_queries():
    pool = AgentPool()
    pool.set_health("log_collector", AgentHealth.OFFLINE)
    assert all(spec.name != "log_collector" for spec in pool.usable())
    assert "log_collector" not in [spec.name for spec in pool.by_capability("log_read")
                                   if spec.usable]


def test_degraded_agent_costs_more_and_is_slower():
    pool = AgentPool()
    before_latency = pool.latency_of("env_prober")
    before_cost = pool.cost_of("env_prober")
    pool.set_health("env_prober", AgentHealth.DEGRADED, factor=0.5)
    assert pool.latency_of("env_prober") > before_latency
    assert pool.cost_of("env_prober") > before_cost


def test_every_read_is_producible():
    """池子自洽性：任何 Agent 要读的键，都必须有人能产出。"""
    pool = AgentPool()
    producible = {key for spec in pool.all() for key in spec.writes}
    for spec in pool.all():
        for key in spec.reads:
            assert key in producible, f"{spec.name} 需要 {key}，但没人产出它"


def test_side_effect_agents_are_high_risk():
    """有副作用却不标高风险，会误导路由与审批。"""
    pool = AgentPool()
    for spec in pool.all():
        if spec.side_effect:
            assert spec.risk is RiskLevel.HIGH, spec.name


# ======================================================================
# §11 标签
# ======================================================================
def _template(name: str = "t", *, author: str = "alice", intent: str = "data_trend",
              labels: list[str] | None = None, steps=None,
              successes: int = 1, runs: int = 1) -> WorkflowTemplate:
    return WorkflowTemplate(
        name=name,
        steps=steps or [WorkflowStep(id="s1", agent="metric_query",
                                     produces=["metrics"])],
        tags=TemplateTags(author=author, source=TemplateSource.LLM_GENERATED,
                          domain="data", intent=intent, labels=labels or []),
        stats=TemplateStats(runs=runs, successes=successes),
    )


def test_template_requires_author_and_intent():
    store = TemplateStore(AgentPool())
    no_author = _template(author="")
    assert store.add(no_author, require_verified=False).decision == "REJECT_NO_AUTHOR"
    no_intent = _template(intent="")
    assert store.add(no_intent, require_verified=False).decision == "REJECT_NO_INTENT"


def test_template_requires_a_successful_run():
    """§12.1 门的第一条：生成者不该自己决定成果够不够格。"""
    store = TemplateStore(AgentPool())
    fresh = _template(successes=0, runs=0)
    outcome = store.add(fresh, require_verified=True)
    assert not outcome.stored and outcome.decision == "REJECT_UNVERIFIED"


def test_structure_hash_ignores_name_and_tags():
    """§12.2 只哈希结构与数据流 —— 同一份 DAG 换个名字还是同一份。"""
    a = _template("甲", labels=["x"])
    b = _template("乙", author="bob", labels=["y"])
    assert a.structure_hash == b.structure_hash


def test_duplicate_structure_is_merged_not_dropped():
    store = TemplateStore(AgentPool())
    first = _template(author="alice", labels=["A"])
    store.add(first, require_verified=False)
    second = _template(author="bob", labels=["B"])
    outcome = store.add(second, require_verified=False)
    assert not outcome.stored and outcome.decision == "REJECT_DUPLICATE"
    merged = store.get(outcome.merged_into)
    assert "alice" in merged.tags.author and "bob" in merged.tags.author
    assert set(merged.tags.labels) == {"A", "B"}


def test_template_with_unknown_agent_is_rejected():
    store = TemplateStore(AgentPool())
    broken = _template(steps=[WorkflowStep(id="s1", agent="ghost_agent")])
    outcome = store.add(broken, require_verified=False)
    assert not outcome.stored and outcome.decision == "REJECT_BROKEN"


def test_label_and_author_search():
    store = TemplateStore(AgentPool())
    store.add(_template("甲", author="alice", labels=["容量", "看板"]), require_verified=False)
    # 注意两条模板的**结构必须不同**，否则会被 §12.2 的结构指纹判为重复
    store.add(
        _template("乙", author="bob", intent="code_review", labels=["安全"],
                  steps=[WorkflowStep(id="s1", agent="code_reader", produces=["code"])]),
        require_verified=False,
    )
    assert [t.name for t in store.by_labels(["容量"])] == ["甲"]
    assert [t.name for t in store.by_author("bob")] == ["乙"]
    assert store.by_labels(["不存在的标签"]) == []


# ======================================================================
# §13 生命周期
# ======================================================================
def test_audit_health_marks_templates_broken_when_agent_goes_offline():
    pool = AgentPool()
    store = TemplateStore(pool)
    store.add(_template(), require_verified=False)
    pool.set_health("metric_query", AgentHealth.OFFLINE)
    report = store.audit_health()
    assert report.broken == [store.all()[0].template_id]


def test_broken_templates_are_not_routable():
    pool = AgentPool()
    store = TemplateStore(pool)
    store.add(_template(), require_verified=False)
    pool.set_health("metric_query", AgentHealth.OFFLINE)
    store.audit_health()
    assert store.by_intent("data_trend") == []


def test_deprecate_keeps_old_version_but_points_forward():
    store = TemplateStore(AgentPool())
    old = _template("v1")
    store.add(old, require_verified=False)
    # 新版必须与旧版结构不同，否则会被去重挡在门外（这一版加了知识检索）
    new = _template("v2", steps=[
        WorkflowStep(id="s1", agent="metric_query", produces=["metrics"]),
        WorkflowStep(id="s2", agent="knowledge_searcher", produces=["references"]),
    ])
    new.template_id = "wf_new"
    store.add(new, require_verified=False)
    assert store.deprecate(old.template_id, new.template_id)
    assert old.tags.state is TemplateState.DEPRECATED
    assert old.tags.superseded_by == "wf_new"
    assert new.based_on == old.template_id and new.version == old.version + 1


def test_seed_templates_are_present_and_healthy():
    from app.pool.templates import seed_templates

    pool = AgentPool()
    seeded = seed_templates(pool)
    assert len(seeded.all()) >= 2
    assert all(t.tags.source is TemplateSource.SEED for t in seeded.all())


def test_eviction_prefers_unused_non_seed_templates():
    """§13.2 淘汰：从「没人用 + 不是种子」里挑 —— 种子是冷启动的兜底。"""
    pool = AgentPool()
    from app.pool.templates import seed_templates

    store = seed_templates(pool)
    store.max_templates = len(store.all()) + 1
    for index in range(3):
        template = _template(f"临时{index}", intent=f"intent_{index}")
        store.add(template, require_verified=False)
    sources = {t.tags.source for t in store.all()}
    assert TemplateSource.SEED in sources, "种子模板不该被淘汰掉"


def test_record_run_updates_statistics():
    store = TemplateStore(AgentPool())
    template = _template("甲")
    store.add(template, require_verified=False)
    store.record_run(template.template_id, True, 500)
    store.record_run(template.template_id, False, 700)
    assert template.stats.runs == 3          # 初始的 1 次 + 刚记的 2 次
    assert template.stats.avg_latency_ms > 0


def test_template_validator_requires_known_agent():
    from app.workflow.validator import WorkflowValidator

    validator = WorkflowValidator(AgentPool())
    ok_template = _template()
    assert validator.validate(ok_template).ok
    bad = _template(steps=[WorkflowStep(id="s1", agent="ghost")])
    codes = {issue.code for issue in validator.validate(bad).blocking}
    assert "UNKNOWN_AGENT" in codes
