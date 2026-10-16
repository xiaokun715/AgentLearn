"""意图库、路由权重与阈值 —— 全章的「判断」都集中在这里。

对齐说明书：
- 五、§5.1 意图库                -> :data:`INTENT_LIBRARY`
- 六、§6.3 评分的四个分量         -> :class:`RoutingPolicy`
- 六、§6.5 决策必须可解释         -> 每个权重都写了它为什么是这个量级
- 十二、§12.1 入库质量门          -> :class:`SavePolicy`
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .enums import RiskLevel


# ======================================================================
# 五、§5.1 意图库
# ======================================================================
@dataclass(frozen=True)
class IntentSpec:
    """一个意图的完整定义。

    ``required_capabilities`` 是这份定义里最重要的一行：它把「用户想要什么」
    翻译成了「系统需要哪些能力」。**路由与编排都不再需要理解自然语言**，
    它们只处理能力集合 —— 这是意图识别层存在的主要理由（§2.4）。

    ``estimated_steps`` 是**结构性的**，不是猜的：它等于「覆盖这些能力最少
    需要几个 Agent」。所以「这个意图天然需要编排」是一个可以算的事实，
    而不是一句主观判断。
    """

    name: str
    description: str
    domain: str
    keywords: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    estimated_steps: int = 1
    risk: RiskLevel = RiskLevel.LOW
    #: 从 query 里抽取槽位用的正则（演示用的最小实现）
    entity_hints: list[str] = field(default_factory=list)


INTENT_LIBRARY: dict[str, IntentSpec] = {
    "troubleshoot_failure": IntentSpec(
        name="troubleshoot_failure",
        description="测试/任务失败，需要定位原因并给出结论",
        domain="troubleshooting",
        keywords=["失败", "排障", "根因", "报错", "为什么失败", "不通过", "定位原因",
                  "failed", "troubleshoot", "root cause", "debug"],
        required_capabilities=[
            "log_read", "log_analysis", "env_check", "config_check", "root_cause", "report",
        ],
        estimated_steps=6,
    ),
    "code_review": IntentSpec(
        name="code_review",
        description="审查代码，重点看安全与质量",
        domain="code_review",
        keywords=["代码", "审查", "review", "安全", "漏洞", "注入", "越权", "code"],
        required_capabilities=["code_read", "security_review", "style_review"],
        estimated_steps=3,
    ),
    "code_security_only": IntentSpec(
        name="code_security_only",
        description="只做安全检查，不关心风格",
        domain="code_review",
        keywords=["安全扫描", "安全审计", "只查安全"],
        required_capabilities=["code_read", "security_review"],
        estimated_steps=2,
    ),
    "data_trend": IntentSpec(
        name="data_trend",
        description="分析某个指标的趋势并出图",
        domain="data",
        keywords=["趋势", "指标", "吞吐", "曲线", "下降", "上升", "波动", "画个图",
                  "trend", "metrics"],
        required_capabilities=["metric_query", "trend_analysis", "chart"],
        estimated_steps=3,
    ),
    "knowledge_lookup": IntentSpec(
        name="knowledge_lookup",
        description="查规范、查历史案例（单步即可）",
        domain="general",
        keywords=["查一下", "检索", "规范", "文档", "历史案例", "标准怎么规定的"],
        required_capabilities=["knowledge_search"],
        estimated_steps=1,
    ),
    "quick_summary": IntentSpec(
        name="quick_summary",
        description="对当前上下文做一次摘要（单步即可）",
        domain="general",
        keywords=["总结", "摘要", "概括", "summarize"],
        required_capabilities=["summarize"],
        estimated_steps=1,
    ),
    "config_fix": IntentSpec(
        name="config_fix",
        description="修正配置并写回环境（**有副作用**）",
        domain="troubleshooting",
        keywords=["修正配置", "改配置", "修复配置", "写回"],
        required_capabilities=["config_check", "config_apply"],
        estimated_steps=2,
        risk=RiskLevel.HIGH,
    ),
}


# ======================================================================
# 六、§6.3 路由评分
# ======================================================================
@dataclass(frozen=True)
class RoutingPolicy:
    """``score = Σ wᵢ · featureᵢ`` 的权重。

    四个分量的量级是刻意排过的：

    * ``coverage`` 权重最高（2.0）—— **能力覆盖是硬约束**。
      一个只覆盖 1/6 能力的直连 Agent，不该因为「便宜」而赢过一条覆盖 6/6 的模板。
    * ``intent_match`` 次之（1.2）—— 模板是**为某个意图而生的**，
      跨意图复用一个形状相似的 DAG 是危险的（输入语义不同，输出就不可信）。
    * ``reliability``（0.8）—— 用平滑后的成功率。**不用原始成功率**，
      理由见 :func:`smoothed_success`。
    * 成本/延迟/风险/步数是**惩罚项**，量级都在 1.0 以下：
      它们只应该在「能力覆盖相当」的时候才起决定作用。
    """

    w_coverage: float = 2.0
    w_intent: float = 1.2
    w_reliability: float = 0.8
    w_reuse: float = 0.4
    penalty_cost: float = 0.6
    penalty_latency: float = 0.5
    penalty_risk: float = 0.35
    penalty_steps: float = 0.25

    #: 低于这个分数就不路由，改成澄清或拒绝（§5.3）
    clarify_threshold: float = 0.85
    #: 意图置信度低于这个值直接进澄清，不浪费一次完整的候选计算
    min_intent_confidence: float = 0.25
    #: 模板被认定的「成熟」阈值：跑够这么多次之后，复用奖励才给满
    mature_runs: int = 20

    def __post_init__(self) -> None:  # pragma: no cover - 纯文档性校验
        if self.w_coverage <= self.w_intent:
            raise ValueError("coverage 权重必须高于 intent 权重：能力覆盖是硬约束")


ROUTING = RoutingPolicy()


def smoothed_success(successes: int, runs: int) -> float:
    """拉普拉斯平滑后的成功率 ``(s+1)/(n+2)``。

    为什么不能用原始成功率：一份刚跑成功一次的新模板，原始成功率是 **100%**，
    它会立刻在评分上压过一条跑了 42 次成功 39 次的成熟模板（92.9%）。
    而这两者的证据量差了 40 倍。

    平滑之后：1/1 → 66.7%，39/42 → 90.9% —— 排序终于反映了「证据够不够」，
    而不是「运气好不好」。
    """
    return (successes + 1) / (runs + 2)


def reuse_bonus(runs: int, mature_runs: int) -> float:
    """复用奖励：对数增长，跑得越多奖励越高，但**有上限**。

    线性的「用得越多越优先」会让模板库锁死在最初的几条上 ——
    新模板永远没有机会被验证，于是永远没有数据去证明它更好。
    对数增长 + 上限，是「倾向成熟」与「给新东西留机会」之间的折中。
    """
    import math

    if runs <= 0:
        return 0.0
    ceiling = math.log1p(mature_runs)
    return min(1.0, math.log1p(runs) / ceiling)


# ======================================================================
# 十二、§12.1 入库质量门
# ======================================================================
@dataclass(frozen=True)
class SavePolicy:
    #: 是否要求「先成功跑过一次」才允许入库
    require_verified: bool = True
    #: 默认标签：新模板自动带上这些，便于检索
    default_labels: tuple[str, ...] = ("现场生成",)
    #: 生成人标签在缺省时用什么
    fallback_author: str = "composer"


SAVE_POLICY = SavePolicy()
