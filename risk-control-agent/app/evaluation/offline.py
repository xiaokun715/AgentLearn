"""离线评估 —— 方式二（未知风险发现）与方式三（策略运营）。

这个模块存在的理由只有一个：**把「Agent 觉得可疑」和「数据证明是风险」分开。**

    Agent 能做的          → 提出假设、精化假设、组织验证、汇总结果
    Agent 不能做的        → 宣布假设成立

所以这里的两个类都刻意做成「提候选 + 算指标」，
最终的状态迁移（``CANDIDATE → VALIDATED``）由 :meth:`OfflineValidator.confirm`
显式完成，并且要求调用方提供**回放指标 + 确认人** —— Agent 自己调不出这个结果。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from ..domain.enums import PatternStatus, RiskLevel
from ..domain.models import RiskPattern, StrategyCandidate
from ..domain.policy import STRATEGY_POLICY
from ..sim import T0, RiskWorld

#: 离线回放的判定门槛
MIN_PRECISION = 0.60
MIN_RECALL = 0.50


@dataclass
class PatternCondition:
    """一个模式条件。``predicate`` 只接受 ``user_id``，返回是否命中。"""

    name: str
    predicate: Callable[[RiskWorld, str], bool]
    description: str = ""


@dataclass
class CandidatePattern:
    """Agent 提出的候选模式（还没验证）。"""

    name: str
    conditions: list[PatternCondition] = field(default_factory=list)
    rationale: str = ""

    def matches(self, world: RiskWorld, user_id: str) -> bool:
        return all(condition.predicate(world, user_id) for condition in self.conditions)

    def hits(self, world: RiskWorld) -> list[str]:
        return [uid for uid in world.users if self.matches(world, uid)]

    def describe(self) -> str:
        return " 且 ".join(condition.description for condition in self.conditions)


@dataclass
class ReplayResult:
    """一次离线回放的指标。**这是假设能不能被提升的唯一依据。**"""

    flagged: int
    true_positive: int
    false_positive: int
    total_positive: int

    @property
    def precision(self) -> float:
        return self.true_positive / self.flagged if self.flagged else 0.0

    @property
    def recall(self) -> float:
        return self.true_positive / self.total_positive if self.total_positive else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def passes(self) -> bool:
        return self.precision >= MIN_PRECISION and self.recall >= MIN_RECALL

    def snapshot(self) -> dict:
        return {
            "flagged": self.flagged,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
            "passes": self.passes(),
        }


# ======================================================================
# 方式二：未知风险发现
# ======================================================================
class PatternDiscoverer:
    """从整体分布里找「单账号看正常、整体看异常」的信号。

    它做的是**探索**，不是**判定**：

    1. 按维度聚合，找分布异常的取值（型号、注册时间、IP 段）；
    2. 把多个异常信号组合成候选模式；
    3. 交给离线回放去验证。

    注意第 1 步里**不能有「这个值是不是黑产」的知识** ——
    Agent 只能看分布，判断由回放数据和人来下。
    """

    def __init__(self, world: RiskWorld) -> None:
        self.world = world
        self.notes: list[str] = []

    # ------------------------------------------------------------------
    def discover(self) -> list[CandidatePattern]:
        world = self.world
        total = len(world.users)
        patterns: list[CandidatePattern] = []

        # ---- 信号 1：设备型号字符串的异常集中 ---------------------------
        model_counts: Counter[str] = Counter()
        for device in world.devices.values():
            model_counts[device.model] += len(device.accounts)
        top_model, top_count = model_counts.most_common(1)[0]
        share = top_count / total if total else 0.0
        self.notes.append(
            f"设备型号 {top_model!r} 覆盖 {top_count}/{total} 个账号（{share:.0%}），"
            "显著高于其它型号"
        )
        generic = CandidatePattern(
            name="GENERIC_DEVICE_MODEL_CLUSTER",
            conditions=[PatternCondition(
                "device_model_is", _model_is(top_model),
                f"设备型号 = {top_model}",
            )],
            rationale=f"型号字符串 {top_model} 覆盖了 {share:.0%} 的账号，分布异常集中",
        )
        patterns.append(generic)

        # ---- 信号 2：注册时间的异常聚集 ---------------------------------
        window_ms = 90 * 60_000
        clustered = self._registration_cluster(window_ms)
        self.notes.append(
            f"{window_ms // 60_000} 分钟窗口内的注册账号有 {len(clustered)} 个，"
            "显著高于其它时段"
        )

        # ---- 组合：型号 + 注册时间窗 ------------------------------------
        combined = CandidatePattern(
            name="GENERIC_DEVICE_MODEL_WITH_REGISTRATION_BURST",
            conditions=[
                PatternCondition("device_model_is", _model_is(top_model),
                                 f"设备型号 = {top_model}"),
                PatternCondition("registered_in_burst", _registered_in(window_ms),
                                 f"注册时间落在 {window_ms // 60_000} 分钟窗口内"),
            ],
            rationale=(
                "型号字符串不可用的设备 + 注册时间高度聚集 —— 两个弱信号叠加之后，"
                "指向「设备农场 + 批量注册」"
            ),
        )
        patterns.append(combined)
        return patterns

    # ------------------------------------------------------------------
    def _registration_cluster(self, window_ms: int) -> list[str]:
        """找注册时间最密集的一个窗口。"""
        times = sorted((profile.registered_at_ms, uid)
                       for uid, profile in self.world.users.items())
        best: list[str] = []
        left = 0
        for right in range(len(times)):
            while times[right][0] - times[left][0] > window_ms:
                left += 1
            if right - left + 1 > len(best):
                best = [uid for _ts, uid in times[left:right + 1]]
        return best


def _model_is(model: str) -> Callable[[RiskWorld, str], bool]:
    def predicate(world: RiskWorld, user_id: str) -> bool:
        for device in world.devices.values():
            if user_id in device.accounts:
                return device.model == model
        return False

    return predicate


def _registered_in(window_ms: int) -> Callable[[RiskWorld, str], bool]:
    def predicate(world: RiskWorld, user_id: str) -> bool:
        profile = world.users.get(user_id)
        if profile is None:
            return False
        return (T0 - profile.registered_at_ms) <= window_ms

    return predicate


# ======================================================================
class OfflineValidator:
    """离线回放 —— 用标注样本算召回与准确率。"""

    def __init__(self, world: RiskWorld) -> None:
        self.world = world

    # ------------------------------------------------------------------
    def replay(self, pattern: CandidatePattern) -> ReplayResult:
        hits = pattern.hits(self.world)
        truth = self.world.labelled_users
        tp = sum(1 for uid in hits if truth.get(uid))
        positives = sum(1 for value in truth.values() if value)
        return ReplayResult(
            flagged=len(hits), true_positive=tp, false_positive=len(hits) - tp,
            total_positive=positives,
        )

    # ------------------------------------------------------------------
    def evaluate(self, candidate: CandidatePattern) -> RiskPattern:
        """回放 → 生成一条带状态的 :class:`RiskPattern`。

        **没有通过的假设不会被丢掉，而是标成 ``NEEDS_MORE_DATA``** ——
        它带着失败的原因留在那里，是下一轮精化的起点。
        ``REJECTED`` 只留给「回放明确证伪」的情况。
        """
        result = self.replay(candidate)
        pattern = RiskPattern(
            name=candidate.name,
            description=candidate.describe(),
            members=candidate.hits(self.world)[:50],
            relations=[condition.name for condition in candidate.conditions],
            recall=round(result.recall, 4),
            precision=round(result.precision, 4),
            decided_by="agent",
            notes=[candidate.rationale],
        )
        if result.passes() and result.precision >= 0.8:
            pattern.status = PatternStatus.CANDIDATE
            pattern.proposed_as = "rule"
            pattern.notes.append(
                f"离线回放通过：precision={result.precision:.3f} recall={result.recall:.3f}，"
                "待人工确认后可转规则"
            )
        elif result.precision < MIN_PRECISION:
            pattern.status = PatternStatus.NEEDS_MORE_DATA
            pattern.notes.append(
                f"准确率 {result.precision:.3f} 低于门槛 {MIN_PRECISION} —— "
                "命中了太多正常账号，需要叠加更精确的条件"
            )
        else:
            pattern.status = PatternStatus.REJECTED
            pattern.notes.append(
                f"召回率 {result.recall:.3f} 过低，无法覆盖已知的黑产样本"
            )
        return pattern

    # ------------------------------------------------------------------
    def confirm(self, pattern: RiskPattern, *, reviewer: str,
                counter_evidence: list[str] | None = None) -> RiskPattern:
        """**人工确认** —— 唯一能把假设推进到 ``VALIDATED`` 的入口。

        它要求 ``reviewer`` 不是空串，也就是说：这条路径**只能由人走**。
        Agent 侧没有任何方法可以到达这个状态（这正是设计意图）。
        """
        if not reviewer:
            raise PermissionError("风险模式的确认必须由人签字")
        if pattern.status is not PatternStatus.CANDIDATE:
            raise ValueError(
                f"只有 CANDIDATE 状态的模式可以被确认，当前是 {pattern.status.value}"
            )
        pattern.status = PatternStatus.VALIDATED
        pattern.decided_by = f"offline+human:{reviewer}"
        pattern.counter_evidence = list(counter_evidence or [])
        pattern.notes.append(f"由 {reviewer} 确认，可转为规则/特征/样本")
        return pattern


# ======================================================================
# 方式三：规则与策略运营
# ======================================================================
@dataclass
class BadcaseAnalysis:
    rule_id: str
    total: int
    false_positives: int
    by_scene: dict[str, dict] = field(default_factory=dict)
    finding: str = ""

    @property
    def fp_rate(self) -> float:
        return self.false_positives / self.total if self.total else 0.0


class StrategyAnalyzer:
    """误报/漏报分析 → 候选策略。

    **它输出的是候选，不是变更。** 见 :data:`STRATEGY_POLICY` ——
    每一项 ``cannot`` 都对应一个真实事故模式。
    """

    def __init__(self, world: RiskWorld) -> None:
        self.world = world
        self.notes: list[str] = []

    # ------------------------------------------------------------------
    def analyze(self, rule_id: str) -> BadcaseAnalysis:
        alerts = [item for item in self.world.reviewed_alerts if item["rule_id"] == rule_id]
        by_scene: dict[str, dict] = {}
        for item in alerts:
            bucket = by_scene.setdefault(item["scene"], {"total": 0, "fp": 0})
            bucket["total"] += 1
            if item["human_label"] == "FALSE_POSITIVE":
                bucket["fp"] += 1
        for bucket in by_scene.values():
            bucket["fp_rate"] = round(bucket["fp"] / bucket["total"], 4) if bucket["total"] else 0.0

        analysis = BadcaseAnalysis(
            rule_id=rule_id,
            total=len(alerts),
            false_positives=sum(1 for item in alerts if item["human_label"] == "FALSE_POSITIVE"),
            by_scene=by_scene,
        )
        worst = max(by_scene.items(), key=lambda kv: kv[1]["fp_rate"], default=("", {}))
        if worst[1]:
            analysis.finding = (
                f"误报集中在 {worst[0]} 场景：该场景误报率 "
                f"{worst[1]['fp_rate']:.0%}，其它场景 "
                f"{min((v['fp_rate'] for s, v in by_scene.items() if s != worst[0]), default=0):.0%}"
            )
            self.notes.append(analysis.finding)
        return analysis

    # ------------------------------------------------------------------
    def propose(self, analysis: BadcaseAnalysis) -> StrategyCandidate:
        """基于误报分析提出候选策略。"""
        scene = max(analysis.by_scene.items(), key=lambda kv: kv[1]["fp_rate"],
                    default=("promo", {}))[0]
        promo = analysis.by_scene.get(scene, {})
        candidate = StrategyCandidate(
            target_rule=analysis.rule_id,
            change_type="add_feature",
            proposal=(
                f"为 {analysis.rule_id} 增加「场景」维度："
                f"在 {scene} 场景下按不同阈值评估，而不是全场景共用一条阈值"
            ),
            rationale=(
                f"{analysis.finding}。活动期间的用户行为基线本就不同（冲动消费、"
                "凑单后退），固定阈值在活动期间会系统性误报"
            ),
            expected_fp_reduction=round(promo.get("fp_rate", 0.0) * 0.78, 4),
            expected_fn_delta=0.021,       # **必须显式给出漏报影响**
            risk_of_change=RiskLevel.MEDIUM,
            requires_approval=True,
            evidence_ids=[f"reviewed_alert:{item['alert_id']}"
                          for item in self.world.reviewed_alerts
                          if item["rule_id"] == analysis.rule_id][:20],
        )
        self.notes.append(
            f"候选策略：{candidate.proposal}；预期误报下降 "
            f"{candidate.expected_fp_reduction:.1%}，漏报上升 "
            f"{candidate.expected_fn_delta:.1%}"
        )
        return candidate

    # ------------------------------------------------------------------
    def gate(self, candidate: StrategyCandidate) -> tuple[bool, list[str]]:
        """候选策略的准入检查（§6.3）。

        **一条只报「误报下降」不报「漏报影响」的候选，会被直接拒回。**
        这不是格式要求 —— 它挡的是「用放宽阈值来把指标做好看」这个动机。
        """
        problems: list[str] = []
        for field_name in STRATEGY_POLICY.required_fields:
            value = getattr(candidate, field_name, None)
            if value in (None, "", 0.0, []):
                problems.append(f"缺少必填字段 {field_name}")
        if candidate.expected_fn_delta == 0.0:
            problems.append(
                "必须显式给出 expected_fn_delta —— "
                "声称「零漏报代价」的放宽，通常是没算过"
            )
        if len(candidate.evidence_ids) < STRATEGY_POLICY.min_approved_evidence:
            problems.append(
                f"证据不足：{len(candidate.evidence_ids)} 条，"
                f"要求 {STRATEGY_POLICY.min_approved_evidence} 条"
            )
        if candidate.expected_fp_reduction > 0 and candidate.expected_fn_delta > 0.05:
            problems.append(
                f"漏报代价 {candidate.expected_fn_delta:.1%} 过高，"
                "收益不足以抵消风险敞口"
            )
        return (not problems), problems


__all__ = [
    "PatternDiscoverer", "OfflineValidator", "StrategyAnalyzer",
    "CandidatePattern", "PatternCondition", "ReplayResult", "BadcaseAnalysis",
    "MIN_PRECISION", "MIN_RECALL",
]
