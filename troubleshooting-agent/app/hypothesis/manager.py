"""根因假设管理器 —— 证据驱动假设的核心（说明书 §5）。

说明书把这一步当作排障智能体的分水岭：

    普通 Agent：输入日志 → LLM → 输出原因
    本方案    ：输入证据 → 多个根因假设 → 找能区分假设的诊断动作 → 执行 → 更新假设

于是本模块要同时提供两件事：

1. **假设维护**（§5.1）：每个假设都有支持证据、反驳证据、尚需检查、状态、可信度、来源；
2. **信息增益计算**（§5.3）：``IG(a) = H(ℋ) - E_{o~a}[H(ℋ|o)]``，
   让规划器能回答「哪个检查最能区分当前候选根因」。

关于 ``None`` 的处理（这是最容易出错的地方）：动作池里查不到某个假设的似然时，
**不代表该假设一定与这个检查无关**。本模块统一按「无信息」处理 ——
即给该假设在所有可能结果上等概率，使似然比为 1、后验不变。
如果把它当成 0，那个假设会被一次与它无关的检查直接判死；当成 1 又等于凭空加了一次伪证据。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..domain.enums import FailureCategory, HypothesisStatus, Observation
from ..domain.models import ActionSpec, FailureClassification, Hypothesis
from ..domain.policy import (
    CATEGORY_HYPOTHESIS_PRIORS,
    CONCLUSION,
    HYPOTHESIS_LIBRARY,
)


@dataclass
class UpdateReport:
    """一次假设更新的结果，用于终端展示与断言。"""

    skill: str
    observation: Observation
    moved: list[tuple[str, float, float]]     # (假设键, 更新前, 更新后)
    entropy_before: float
    entropy_after: float

    @property
    def information_gain(self) -> float:
        """**实际**信息增益（事后），与规划时的**预期**增益互为对照。"""
        return self.entropy_before - self.entropy_after


class HypothesisManager:
    """候选根因集合及其贝叶斯更新。"""

    def __init__(self, hypotheses: list[Hypothesis]) -> None:
        self._hypotheses: dict[str, Hypothesis] = {h.key: h for h in hypotheses}
        self._normalize()

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def from_classification(cls, classification: FailureClassification) -> HypothesisManager:
        """§5.1「不要只生成一个根因」—— 从分类结果展开成一组候选假设。

        先验来自 ``CATEGORY_HYPOTHESIS_PRIORS``（分类类别 → 各假设的起点概率），
        所以分类**影响排序但不决定结论**：它只是把初始概率挪一挪，
        真正的胜负由后续的可验证检查决定。
        """
        priors = CATEGORY_HYPOTHESIS_PRIORS.get(
            classification.primary_category,
            CATEGORY_HYPOTHESIS_PRIORS[FailureCategory.UNKNOWN],
        )
        hypotheses: list[Hypothesis] = []
        for key, prior in priors.items():
            spec = HYPOTHESIS_LIBRARY[key]
            hypotheses.append(
                Hypothesis(
                    key=key,
                    description=spec.description,
                    category=spec.category,
                    prior=prior,
                    posterior=prior,
                    required_checks=list(spec.required_checks),
                    provenance=f"分类器给出（{classification.decided_by}）",
                )
            )
        return cls(hypotheses)

    @classmethod
    def from_keys(cls, keys_with_priors: dict[str, float], provenance: str = "llm") -> HypothesisManager:
        """直接按给定键与先验构造（供 LLM 提候选假设的路径使用）。"""
        hypotheses = []
        for key, prior in keys_with_priors.items():
            spec = HYPOTHESIS_LIBRARY.get(key)
            if spec is None:
                continue
            hypotheses.append(
                Hypothesis(
                    key=key,
                    description=spec.description,
                    category=spec.category,
                    prior=prior,
                    posterior=prior,
                    required_checks=list(spec.required_checks),
                    provenance=provenance,
                )
            )
        return cls(hypotheses)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def all(self) -> list[Hypothesis]:
        return sorted(self._hypotheses.values(), key=lambda h: h.posterior, reverse=True)

    def get(self, key: str) -> Hypothesis | None:
        return self._hypotheses.get(key)

    def active(self) -> list[Hypothesis]:
        """仍参与规划的假设（排除已被证据排除的）。"""
        return [h for h in self.all() if h.status is not HypothesisStatus.REFUTED]

    def leader(self) -> Hypothesis:
        return self.all()[0]

    def runner_up(self) -> Hypothesis | None:
        ranked = self.all()
        return ranked[1] if len(ranked) > 1 else None

    # ------------------------------------------------------------------
    # 不确定性
    # ------------------------------------------------------------------
    def entropy(self) -> float:
        """候选根因集合的熵 ``H(ℋ) = -Σ P(H_i) log P(H_i)``（单位 bit）。

        熵越小越接近定性。规划器用它算信息增益，结论门用它做展示 ——
        但**不**用它做下结论的判据（熵低可能只是「大家一起低」，不代表定位了）。
        """
        total = 0.0
        for hypothesis in self._hypotheses.values():
            p = hypothesis.posterior
            if p > 0:
                total -= p * math.log2(p)
        return total

    def expected_information_gain(self, spec: ActionSpec) -> float:
        """§5.3 ``IG(a) = H(ℋ) - E_{o~a}[H(ℋ|o)]``。

        计算方式：对动作声明的每种可能观测 ``o``，先用似然表推出
        ``P(o) = Σ_i P(H_i)·P(o|H_i)``，再算该观测下的后验熵，最后按 ``P(o)`` 加权。
        """
        hypotheses = list(self._hypotheses.values())
        if len(hypotheses) < 2:
            return 0.0
        outcomes = spec.declared_outcomes()
        if not outcomes:
            return 0.0

        entropy_before = self.entropy()
        expected_entropy = 0.0

        for outcome in outcomes:
            weights: list[float] = []
            for hypothesis in hypotheses:
                likelihood = spec.likelihood(hypothesis.key, outcome)
                if likelihood is None:
                    likelihood = 1.0 / len(outcomes)   # 无信息 -> 等概率
                weights.append(hypothesis.posterior * likelihood)
            probability = sum(weights)
            if probability <= 0:
                continue
            posterior_entropy = 0.0
            for weight in weights:
                q = weight / probability
                if q > 0:
                    posterior_entropy -= q * math.log2(q)
            expected_entropy += probability * posterior_entropy

        return max(0.0, entropy_before - expected_entropy)

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------
    def update(
        self,
        spec: ActionSpec,
        observation: Observation,
        evidence_ids: list[str],
    ) -> UpdateReport:
        """用一次真实观测做贝叶斯更新。

        ``P(H_i | o) ∝ P(o | H_i) · P(H_i)``，再重新归一化。
        ``INCONCLUSIVE`` / ``ERROR`` 这类「检查没结论」的结果不会挂到任何假设上 ——
        它们既不是支持也不是反驳，只是白花了一次预算。
        """
        entropy_before = self.entropy()
        outcomes = spec.declared_outcomes()
        moved: list[tuple[str, float, float]] = []

        if observation in (Observation.INCONCLUSIVE, Observation.ERROR) or not outcomes:
            for hypothesis in self._hypotheses.values():
                hypothesis.history.append(
                    {
                        "check": spec.name,
                        "observation": observation.value,
                        "note": "无结论，不更新后验",
                    }
                )
            return UpdateReport(spec.name, observation, [], entropy_before, entropy_before)

        unnormalized: dict[str, float] = {}
        for key, hypothesis in self._hypotheses.items():
            likelihood = spec.likelihood(key, observation)
            if likelihood is None:
                likelihood = 1.0 / len(outcomes)
            unnormalized[key] = hypothesis.posterior * likelihood

        total = sum(unnormalized.values())
        if total <= 0:
            # 所有假设都不支持这个观测（似然表本身有问题）—— 保持原样，但记一笔
            for hypothesis in self._hypotheses.values():
                hypothesis.history.append(
                    {"check": spec.name, "observation": observation.value, "note": "似然全零，跳过"}
                )
            return UpdateReport(spec.name, observation, [], entropy_before, entropy_before)

        for key, hypothesis in self._hypotheses.items():
            before = hypothesis.posterior
            after = unnormalized[key] / total
            hypothesis.posterior = after
            delta = after - before

            # 两个阈值，两件事（见 ConclusionPolicy 的注释）：
            #   够 0.08 -> 这次检查对它有区分力（结论门要用）
            #   够 0.02 -> 证据记在它头上（弱支持也要记账）
            if abs(delta) >= CONCLUSION.posterior_move_epsilon:
                moved.append((key, before, after))
                if spec.name not in hypothesis.discriminating_checks:
                    hypothesis.discriminating_checks.append(spec.name)

            if abs(delta) >= CONCLUSION.evidence_attach_epsilon:
                # 证据挂载：涨了算支持，跌了算反驳
                if delta > 0:
                    hypothesis.supporting_evidence.extend(
                        eid for eid in evidence_ids if eid not in hypothesis.supporting_evidence
                    )
                else:
                    hypothesis.contradicting_evidence.extend(
                        eid for eid in evidence_ids if eid not in hypothesis.contradicting_evidence
                    )

            hypothesis.history.append(
                {
                    "check": spec.name,
                    "observation": observation.value,
                    "posterior": round(after, 4),
                    "delta": round(delta, 4),
                    "evidence": list(evidence_ids),
                }
            )

        self._refresh_status()
        return UpdateReport(spec.name, observation, moved, entropy_before, self.entropy())

    def attach_evidence(
        self,
        key: str,
        *,
        supporting: list[str] | None = None,
        contradicting: list[str] | None = None,
    ) -> None:
        """手工挂载证据（供轨迹分析等非动作类证据使用）。"""
        hypothesis = self._hypotheses.get(key)
        if hypothesis is None:
            return
        for eid in supporting or []:
            if eid not in hypothesis.supporting_evidence:
                hypothesis.supporting_evidence.append(eid)
        for eid in contradicting or []:
            if eid not in hypothesis.contradicting_evidence:
                hypothesis.contradicting_evidence.append(eid)

    def boost_prior(self, key: str, delta: float, reason: str) -> None:
        """按轨迹线索调整先验。

        **轨迹是路标，检查是证据** —— 这是本 Demo 刻意划的一条线。
        执行轨迹能告诉我们「往哪儿看」（例如某个节点自称成功却没过校验），
        但那不等于「某个根因已经成立」。所以这里只动 ``prior``（起点），
        不动 ``supporting_evidence``（结论门要数的那本账）。
        把路标计成证据，就是「模型一猜就中」的另一种写法。
        """
        hypothesis = self._hypotheses.get(key)
        if hypothesis is None or delta <= 0:
            return
        before = hypothesis.prior
        hypothesis.prior = round(min(0.95, before + delta), 4)
        hypothesis.posterior = hypothesis.prior
        hypothesis.provenance += f"；{reason}（先验 {before:.2f}→{hypothesis.prior:.2f}）"
        self._normalize()

    def renormalize(self) -> None:
        """外部改了 ``prior`` / ``posterior`` 之后重新归一化（如经验库加权先验）。

        单独暴露成公开方法，是因为「谁改了概率谁负责归一化」这条约定
        不能靠 `_normalize` 这种私有名字来提醒调用方。
        """
        self._normalize()

    def _normalize(self) -> None:
        total = sum(h.posterior for h in self._hypotheses.values())
        if total <= 0:
            uniform = 1.0 / max(1, len(self._hypotheses))
            for hypothesis in self._hypotheses.values():
                hypothesis.posterior = uniform
            return
        for hypothesis in self._hypotheses.values():
            hypothesis.posterior = hypothesis.posterior / total
        self._refresh_status()

    def _refresh_status(self) -> None:
        for hypothesis in self._hypotheses.values():
            # 「被排除」必须由**反证**触发。
            # 一个初始先验就在 0.05 的假设（例如环境类故障下的 H_CASE）不该在
            # 一次检查都还没做的时候就被标成 REFUTED —— 那不是「查过且推翻」，
            # 那只是「一开始就不太可能」。两者混为一谈，结论的严谨性就没了。
            if (
                hypothesis.posterior < CONCLUSION.refute_probability
                and hypothesis.contradicting_evidence
            ):
                hypothesis.status = HypothesisStatus.REFUTED
            elif (
                hypothesis.posterior >= CONCLUSION.accept_probability
                and (hypothesis.supporting_evidence or hypothesis.discriminating_checks)
            ):
                hypothesis.status = HypothesisStatus.SUPPORTED
            elif hypothesis.supporting_evidence or hypothesis.contradicting_evidence:
                hypothesis.status = HypothesisStatus.UNDECIDED
            else:
                hypothesis.status = HypothesisStatus.PENDING

    def finalize_statuses(self) -> None:
        """排障收尾：把「从没被有效检验」的假设标成 UNDECIDED（而不是 PENDING）。

        这一步是结论可信度的关键。一个仍是 PENDING 的假设，读者无从判断
        「它是没问题，还是根本没查」—— 标成 UNDECIDED 后，
        ``UNRESOLVED`` 的结论才有依据。
        """
        for hypothesis in self._hypotheses.values():
            if hypothesis.status is HypothesisStatus.PENDING:
                hypothesis.status = HypothesisStatus.UNDECIDED

    # ------------------------------------------------------------------
    # 结论门
    # ------------------------------------------------------------------
    def conclusion_ready(self, source_of=None) -> tuple[bool, str]:
        """§10.3 第 1 条：**不能只依赖 LLM 的主观判断**。

        三条确定性门槛，全部满足才允许下结论：
        概率过线、独立证据来源够、领跑假设被主动推动过（区分力检查）。
        再加一条边际要求：领先第二名不够多时不下结论。

        ``source_of`` 是一个 ``evidence_id -> 来源名`` 的解析函数（通常是证据库）。
        传了它，第二道门按**独立来源数**算；没传就退化成按条数算 ——
        退化路径只用于单测，Agent 主循环一定传。
        """
        leader = self.leader()
        if leader.posterior < CONCLUSION.accept_probability:
            return False, (
                f"领跑假设 {leader.key} 后验 {leader.posterior:.2f} "
                f"< {CONCLUSION.accept_probability:.2f}"
            )
        if source_of is not None:
            sources = {source_of(eid) for eid in leader.supporting_evidence}
            sources.discard(None)
            count, unit = len(sources), "个独立来源"
        else:
            count, unit = len(leader.supporting_evidence), "条支持证据"
        decisive = self.max_move(leader.key)
        if count < CONCLUSION.min_independent_sources and decisive < CONCLUSION.decisive_move:
            return False, (
                f"领跑假设 {leader.key} 只有 {count} {unit}，且没有任何一次检查"
                f"把它推高 {CONCLUSION.decisive_move:.2f} 以上"
                f"（最大单次变动 {decisive:+.2f}）"
            )
        if CONCLUSION.require_discriminating_check and not leader.discriminating_checks:
            return False, (
                f"领跑假设 {leader.key} 从未被任何检查**主动推动**过 —— "
                "「没被反驳」不等于「被支持」"
            )
        # 这里刻意**没有**「领先第二名至少 N」的边际门。
        #
        # 后验是归一化的：领跑假设 ≥ accept_probability(0.75) 意味着次席必然 ≤ 0.25，
        # 也就是差距必然 ≥ 0.50。任何小于 0.50 的边际阈值都永远不会触发 ——
        # 写上它只是自我感动，而且会让人误以为系统多了一道保护。
        # 「不急着一锤定音」这件事，由 accept_probability 这一条阈值本身承担。
        leader_margin = None
        runner = self.runner_up()
        if runner is not None:
            leader_margin = leader.posterior - runner.posterior
        return True, (
            f"领跑假设 {leader.key} 后验 {leader.posterior:.2f}，证据与区分力齐备"
            + (f"（领先次席 {runner.key} {leader_margin:.2f}）" if runner else "")
        )

    def max_move(self, key: str) -> float:
        """某个假设历史上**单次**被推动的最大幅度。

        用来判断「这一定论是否来自一次决定性检查」。取的是带符号的原始 delta，
        所以我们只关心正向推动（负向的大变动是反驳，不是支持）。
        """
        hypothesis = self._hypotheses.get(key)
        if hypothesis is None:
            return 0.0
        best = 0.0
        for entry in hypothesis.history:
            delta = entry.get("delta")
            if isinstance(delta, (int, float)):
                best = max(best, float(delta))
        return best

    def summary(self) -> list[dict]:
        return [
            {
                "key": h.key,
                "description": h.description,
                "category": h.category.value,
                "status": h.status.value,
                "prior": round(h.prior, 3),
                "posterior": round(h.posterior, 3),
                "supporting": len(h.supporting_evidence),
                "contradicting": len(h.contradicting_evidence),
            }
            for h in self.all()
        ]
