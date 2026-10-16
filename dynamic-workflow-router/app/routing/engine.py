"""路由引擎（说明书 §6 / §7）。

它回答**一个问题**：这一次请求该交给谁？

    直连某个子 Agent    快、便宜、没有轨迹
    命中已有模板        可复现、有轨迹、验证过
    现场编排一份新的    什么都能做，但要付出编排成本
    澄清                意图说不清，问一句最便宜
    拒绝                超出能力边界

**为什么不能写成一堆 if-else**（§6.2）：三条件分别成立时该选谁，
是一个多维度的取舍，写成规则之后会迅速退化成「谁最近加的规则谁优先」。
统一评分的好处不是「更聪明」，而是**可解释、可调、可回归** ——
每个候选的每一分量都能打出来（§6.5）。

**一条硬约束**（§6.3）：**部分覆盖的直连 Agent 不合格**。
一个只覆盖了 2/6 能力的 Agent 会自信地回答一个它没被设计来回答的问题，
这比明确失败更糟。所以直连候选的门槛是覆盖率 == 1.0。
"""
from __future__ import annotations

from ..domain.enums import IntentStatus, RouteTarget
from ..domain.models import (
    Intent,
    RouteCandidate,
    RoutingDecision,
    RunRequest,
)
from ..domain.policy import ROUTING, RoutingPolicy, reuse_bonus, smoothed_success
from ..pool.templates import TemplateStore


class RoutingEngine:
    def __init__(self, pool, store: TemplateStore, policy: RoutingPolicy = ROUTING) -> None:
        self.pool = pool
        self.store = store
        self.policy = policy

    # ==================================================================
    def route(self, intent: Intent, request: RunRequest) -> RoutingDecision:
        # ---- 强制模式（§7）------------------------------------------
        if request.mode.value == "FORCE_WORKFLOW":
            # 「强制」就是强制：即使池子里有一条完全匹配的模板，也照样现场生成。
            # 理由见 §7.1 —— 用户选这个模式通常是为了「看看现在能编出什么」，
            # 或者为了做 A/B 对照。偷偷复用模板会让这个语义失效。
            return RoutingDecision(
                target=RouteTarget.COMPOSE_NEW,
                chosen=None,
                chosen_label="现场编排",
                candidates=[],
                rationale="用户选择了 FORCE_WORKFLOW：跳过模板复用，直接现场生成一份新模板",
                forced=True,
            )

        # ---- 意图不可行动 -> 澄清（§5.3）------------------------------
        if intent.status is not IntentStatus.RESOLVED or not intent.actionable:
            return RoutingDecision(
                target=RouteTarget.CLARIFY,
                chosen_label="需要澄清",
                candidates=[],
                rationale=(
                    f"意图状态 {intent.status.value}、置信度 {intent.confidence:.2f} —— "
                    "硬路由到错误的子 Agent 比多问一句贵得多"
                ),
            )

        candidates = self._collect(intent)
        if request.mode.value == "FORCE_DIRECT":
            return self._force_direct(intent, candidates)

        viable = [item for item in candidates if item.blocked_reason is None]
        if not viable:
            # 没有任何候选能覆盖 —— 这**不是**失败，是「需要现场编排」
            return RoutingDecision(
                target=RouteTarget.COMPOSE_NEW,
                chosen_label="现场编排",
                candidates=candidates,
                rejected=[item.label for item in candidates if item.blocked_reason],
                rationale=(
                    "池子里没有能完整覆盖该意图的终点 —— "
                    f"需要 {len(intent.required_capabilities)} 项能力，"
                    "没有任何子 Agent 或模板能一次覆盖"
                ),
            )

        viable.sort(key=lambda item: -item.score)
        best = viable[0]

        if best.score < self.policy.clarify_threshold:
            return RoutingDecision(
                target=RouteTarget.CLARIFY,
                chosen=best.name,
                chosen_label=best.label,
                score=best.score,
                candidates=candidates,
                rationale=(
                    f"最高分 {best.score:.3f} 低于澄清阈值 "
                    f"{self.policy.clarify_threshold} —— 候选之间没有实质差别"
                ),
            )

        target = (
            RouteTarget.EXISTING_TEMPLATE
            if best.kind is RouteTarget.EXISTING_TEMPLATE
            else RouteTarget.DIRECT_AGENT
        )
        return RoutingDecision(
            target=target,
            chosen=best.name,
            chosen_label=best.label,
            score=best.score,
            candidates=candidates,
            rejected=[item.label for item in candidates if item.blocked_reason],
            rationale=(
                f"选中 {best.label}：覆盖 {best.coverage:.2f}、"
                f"意图匹配 {best.intent_match:.2f}、可靠度 {best.reliability:.2f}"
            ),
        )

    # ==================================================================
    # 候选收集与评分（§6.3）
    # ==================================================================
    def _collect(self, intent: Intent) -> list[RouteCandidate]:
        wanted = list(intent.required_capabilities)
        candidates: list[RouteCandidate] = []

        # ---- 直连子 Agent -------------------------------------------
        for spec, coverage in self.pool.covering(wanted):
            blocked = None
            if coverage < 1.0:
                blocked = f"只覆盖 {coverage:.0%} 的能力，回答会不可信"
            if spec.side_effect:
                blocked = "有副作用，不允许被自动路由"
            candidates.append(self._score_agent(spec, coverage, intent, blocked))

        # ---- 已有模板 -------------------------------------------------
        for template in self.store.by_intent(intent.name):
            candidates.append(self._score_template(template, intent))

        return candidates

    def _score_agent(self, spec, coverage: float, intent: Intent,
                     blocked: str | None) -> RouteCandidate:
        max_cost = max((item.cost for item in self.pool.all()), default=1.0)
        max_latency = max(self.pool.max_latency(), 1)
        score = (
            self.policy.w_coverage * coverage
            + self.policy.w_intent * 1.0          # 子 Agent 不与意图绑定，取中性值
            + self.policy.w_reliability * 1.0     # 基础能力，默认可信
            + self.policy.w_reuse * 0.0
            - self.policy.penalty_cost * (spec.cost / max_cost)
            - self.policy.penalty_latency * (spec.latency_ms / max_latency)
            - self.policy.penalty_risk * spec.risk.order
        )
        return RouteCandidate(
            kind=RouteTarget.DIRECT_AGENT,
            name=spec.name,
            label=f"Agent:{spec.name}",
            score=round(score * spec.health_factor, 4),
            coverage=coverage,
            intent_match=1.0,
            reliability=1.0,
            cost=spec.cost,
            latency_ms=spec.latency_ms,
            risk=spec.risk,
            steps=1,
            blocked_reason=blocked,
        )

    def _score_template(self, template, intent: Intent) -> RouteCandidate:
        agents = [self.pool.get(name) for name in template.agents_used()]
        covered: set[str] = set()
        for spec in agents:
            if spec is not None:
                covered |= set(spec.capabilities)
        wanted = set(intent.required_capabilities)
        coverage = len(wanted & covered) / len(wanted) if wanted else 0.0

        blocked = None
        if coverage < 1.0:
            blocked = f"模板只覆盖 {coverage:.0%} 的能力"
        unavailable = [name for name, spec in zip(template.agents_used(), agents, strict=False)
                       if spec is None]
        if unavailable:
            blocked = f"引用了不存在的 Agent：{unavailable}"

        max_cost = max((item.cost for item in self.pool.all()), default=1.0) * len(template.steps)
        max_latency = max(self.pool.max_latency() * len(template.steps), 1)
        cost = template.estimated_cost_total(self.pool)
        latency = template.estimated_latency_ms(self.pool)
        reliability = smoothed_success(template.stats.successes, template.stats.runs)
        reuse = reuse_bonus(template.stats.runs, self.policy.mature_runs)

        score = (
            self.policy.w_coverage * coverage
            + self.policy.w_intent * 1.0
            + self.policy.w_reliability * reliability
            + self.policy.w_reuse * reuse
            - self.policy.penalty_cost * (cost / max(max_cost, 1e-6))
            - self.policy.penalty_latency * (latency / max(max_latency, 1))
            - self.policy.penalty_risk * template.max_risk(self.pool).order
            - self.policy.penalty_steps * (len(template.steps) - 1) / 8.0
        )
        return RouteCandidate(
            kind=RouteTarget.EXISTING_TEMPLATE,
            name=template.template_id,
            label=f"模板:{template.name}",
            score=round(score, 4),
            coverage=coverage,
            intent_match=1.0,
            reliability=round(reliability, 4),
            cost=cost,
            latency_ms=latency,
            risk=template.max_risk(self.pool),
            steps=len(template.steps),
            blocked_reason=blocked,
        )

    # ==================================================================
    def _force_direct(self, intent: Intent, candidates: list[RouteCandidate]) -> RoutingDecision:
        agents = [item for item in candidates if item.kind is RouteTarget.DIRECT_AGENT]
        agents.sort(key=lambda item: (-item.coverage, -item.score))
        if not agents:
            return RoutingDecision(
                target=RouteTarget.REJECT,
                chosen_label="无可用 Agent",
                candidates=candidates,
                rationale="FORCE_DIRECT 但池子里没有任何 Agent 能覆盖该意图的任一能力",
                forced=True,
            )
        best = agents[0]
        note = ""
        if best.coverage < 1.0:
            note = "（注意：覆盖不足，用户主动选择了直连，结果可能不完整）"
        return RoutingDecision(
            target=RouteTarget.DIRECT_AGENT,
            chosen=best.name,
            chosen_label=best.label,
            score=best.score,
            candidates=candidates,
            rationale=f"用户选择 FORCE_DIRECT，按覆盖度取最高的 {best.label}{note}",
            forced=True,
        )

