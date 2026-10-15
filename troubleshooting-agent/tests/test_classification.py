"""故障分类测试（说明书 §4）。

重点验证「规则优先 + LLM 补充」的分层逻辑，以及两条边界：
规则命不中不等于规则失败；LLM 的输出必须过 Schema 校验。
"""
from __future__ import annotations

from app.classification.classifier import RULES, FailureClassifier
from app.domain.enums import FailureCategory
from app.domain.models import FailureClassification, FailureContext, TraceAnalysis


def _classify(platform):
    agent = platform.build_agent()
    trace = agent.analyzer.analyze(platform.world.context)
    return FailureClassifier(llm=platform.llm).classify(platform.world.context, trace)


def test_rule_classifies_explicit_environment_error_code(env_platform):
    result = _classify(env_platform)
    assert result.decided_by == "rule"
    assert result.primary_category is FailureCategory.ENVIRONMENT
    assert FailureCategory.TOOL_EXECUTION in result.secondary_categories
    assert "ENV_NOT_READY" in result.rationale


def test_rule_detects_upstream_false_success(env_platform):
    """即使没有错误码，轨迹里的假成功节点也足以让规则定性。"""
    agent = env_platform.build_agent()
    trace = agent.analyzer.analyze(env_platform.world.context)
    # 抹掉错误码，只剩「上游自称成功却没过校验」这条线索
    for event in env_platform.world.context.execution_trace:
        event.error_code = None
    result = FailureClassifier(llm=env_platform.llm).classify(
        env_platform.world.context, trace
    )
    assert result.decided_by == "rule"
    assert "RULE_FALSE_SUCCESS_UPSTREAM" in result.rationale
    assert result.primary_category is FailureCategory.ENVIRONMENT


def test_rules_do_not_fire_on_ambiguous_failure(make_platform):
    """「预期与实际不一致」既可能是软件问题也可能是用例问题，规则不该硬判。"""
    platform = make_platform("config")
    result = _classify(platform)
    assert result.decided_by == "llm"


def test_llm_confidence_is_discounted(make_platform):
    """LLM 给的置信度未经校准，必须打折（§4.2）。"""
    platform = make_platform("config")
    result = _classify(platform)
    assert result.confidence <= 0.7


def test_unknown_is_allowed_when_evidence_is_thin(make_platform):
    platform = make_platform("sparse")
    result = _classify(platform)
    assert result.primary_category is FailureCategory.UNKNOWN


def test_classifier_returns_unknown_without_llm(make_platform):
    platform = make_platform("config")
    agent = platform.build_agent()
    trace = agent.analyzer.analyze(platform.world.context)
    result = FailureClassifier(llm=None).classify(platform.world.context, trace)
    assert result.decided_by == "unknown"
    assert result.primary_category is FailureCategory.UNKNOWN


def test_invalid_llm_output_falls_back_to_unknown(env_platform):
    """模型返回了一个不存在的类别 —— Schema 校验必须拦下它。"""

    class BrokenLLM:
        def classify_failure(self, ctx, trace):
            return {"primary_category": "COSMIC_RAY_HIT", "confidence": 0.99}

    # 构造一个不会命中任何规则的上下文，确保走到 LLM 分支
    broken = FailureClassifier(llm=BrokenLLM())
    ctx = FailureContext(task_id="T", test_case_id="TC", raw_logs=["nothing here"])
    result = broken.classify(ctx, TraceAnalysis())
    assert result.primary_category is FailureCategory.UNKNOWN
    assert result.decided_by == "unknown"
    assert "Schema" in result.rationale


def test_rules_only_read_deterministic_signals():
    """每条规则都必须能用纯函数式的判据描述（便于回归测试）。"""
    for rule in RULES:
        assert rule.name.startswith("RULE_")
        assert rule.description
        assert callable(rule.matches)


def test_classification_result_is_validated_by_pydantic():
    """分类结果本身是一个受约束的契约，不是自由字典。"""
    result = FailureClassification(primary_category=FailureCategory.ENVIRONMENT)
    assert result.primary_category is FailureCategory.ENVIRONMENT
    assert result.confidence == 0.0
    assert result.decided_by == "rule"
