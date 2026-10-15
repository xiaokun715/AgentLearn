"""故障经验库 —— 沉淀「经过验证的故障模式与有效诊断路径」（说明书 §8）。

说明书对这件事有一个很强的主张：

> 不建议让 Agent 每次排障完成后，直接把总结写入长期经验库。

理由很实在：刚排完障的 Agent 最自信，也最容易把「推断」当「结论」存下来 ——
下次排障就会拿着一个幻觉当前提。所以 :meth:`ExperienceManager.extract` 走的是一条
**质量门控流水线**，每一环都可能把候选经验退回去：

    排障结束 → 提取候选经验 → 检查证据是否完整 → 判断根因是否经过验证
             → 检查是否与已有案例重复 → 检查适用的软件版本和环境
             → 生成候选经验 → 质量门控 → 发布到经验库

三类经验（§8.2）全部保留，**尤其是 ``FAILED``**：

> 尤其需要保留失败经验，否则系统可能反复尝试已经证明无效的方法。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import ExperienceStatus, TerminalState
from ..domain.models import (
    DiagnosisConclusion,
    ExperienceCase,
    FailureContext,
    VerifiedRootCause,
    stable_hash,
)
from ..evidence.store import EvidenceStore
from ..hypothesis.manager import HypothesisManager


@dataclass
class Extraction:
    """一次经验提取的结果 —— 入没入库、为什么。"""

    case: ExperienceCase | None
    decision: str
    reasons: list[str] = field(default_factory=list)

    @property
    def stored(self) -> bool:
        return self.case is not None and self.decision.startswith("PUBLISH")


@dataclass
class Retrieval:
    """一条检索命中。``applicable`` 为 False 时**不可复用其恢复动作**。"""

    case: ExperienceCase
    score: float
    applicable: bool
    reason: str


class ExperienceManager:
    """故障经验的提取、检索与复用。"""

    def __init__(self) -> None:
        self._cases: dict[str, ExperienceCase] = {}
        self._by_signature: dict[str, str] = {}
        #: 失败经验的去重索引：(指纹, 动作集合) -> case_id
        self._failed_index: dict[str, str] = {}
        self.audit: list[dict] = []

    # ==================================================================
    # 检索（§8.3）
    # ==================================================================
    def retrieve(
        self,
        ctx: FailureContext,
        *,
        min_score: float = 0.30,
        limit: int = 3,
    ) -> list[Retrieval]:
        """按故障指纹检索历史案例，并**逐条校验适用条件**。

        说明书 §8.3 的最后一句是整节的落点：
        「不能因为两个任务的错误描述相似，就直接套用相同的恢复操作。」
        所以这里返回的每一命中都带 ``applicable`` 与判据，由调用方决定用不用。
        """
        signature = ctx.signature()
        results: list[Retrieval] = []
        for case in self._cases.values():
            score = signature.match_score(case.failure_signature)
            if score < min_score:
                continue
            applicable, reason = case.applicable_to(ctx)
            results.append(
                Retrieval(case=case, score=round(score, 3),
                          applicable=applicable, reason=reason)
            )
        results.sort(key=lambda item: (item.applicable, item.score), reverse=True)
        return results[:limit]

    def seed_priors(
        self,
        manager: HypothesisManager,
        retrievals: list[Retrieval],
        *,
        strength: float = 0.35,
    ) -> list[str]:
        """用**已验证且适用**的历史案例给假设先验加权。

        只调先验、不调后验，也不直接下结论 —— 经验是「更快找到方向」的工具，
        不是「跳过验证」的许可证。这也让 §12 第三问（怎么证明经验真的有用）
        有可测量的对照：加权前后的诊断步数会不一样。
        """
        notes: list[str] = []
        for item in retrievals:
            if not item.applicable:
                notes.append(f"跳过 {item.case.case_id}：{item.reason}")
                continue
            if item.case.status is not ExperienceStatus.VERIFIED:
                notes.append(f"跳过 {item.case.case_id}：状态 {item.case.status.value}，不作为先验依据")
                continue
            root = item.case.verified_root_cause
            if root is None:
                continue
            hypothesis = manager.get(root.key)
            if hypothesis is None:
                continue
            before = hypothesis.prior
            hypothesis.prior = round(min(0.95, before + strength * item.score), 4)
            hypothesis.posterior = hypothesis.prior
            hypothesis.provenance += f"；经验 {item.case.case_id} 加权（{before:.2f}→{hypothesis.prior:.2f}）"
            item.case.reused_count += 1
            notes.append(
                f"历史案例 {item.case.case_id} 加权 {root.key} 先验 {before:.2f}→{hypothesis.prior:.2f}"
            )
        manager.renormalize()   # 先验调整后必须重新归一化
        return notes

    def avoid_actions(self, ctx: FailureContext) -> list[str]:
        """从**失败经验**里提取「不要再试」的动作清单（§8.2）。"""
        avoided: list[str] = []
        for case in self._cases.values():
            if case.status is not ExperienceStatus.FAILED:
                continue
            if ctx.signature().match_score(case.failure_signature) < 0.5:
                continue
            for action in case.recovery_actions:
                if action not in avoided:
                    avoided.append(action)
        return avoided

    # ==================================================================
    # 提取与质量门控（§8.2）
    # ==================================================================
    def extract(
        self,
        conclusion: DiagnosisConclusion,
        ctx: FailureContext,
        store: EvidenceStore,
        *,
        attempts: int = 1,
    ) -> Extraction:
        reasons: list[str] = []

        # ---- 门 1：缺失必要输入 / 完全没定位的，不入库 -------------------
        if conclusion.terminal_state is TerminalState.INVALID_INPUT:
            return Extraction(None, "REJECT_INVALID_INPUT",
                              ["输入不完整，排障未真正开始"])
        if conclusion.terminal_state is TerminalState.UNRESOLVED:
            return Extraction(None, "REJECT_UNRESOLVED",
                              ["未定位根因 —— 只保留证据，不产生经验"])

        # ---- 门 2：证据是否完整 -----------------------------------------
        supporting = list(conclusion.evidence_ids)
        raw_ancestors = store.raw_ancestors(supporting)
        if conclusion.root_cause_key is None:
            return Extraction(None, "REJECT_NO_ROOT_CAUSE", ["结论里没有根因"])
        if len(supporting) < 2:
            reasons.append(f"证据条数不足（{len(supporting)} < 2）")
        if not raw_ancestors:
            reasons.append("没有可回溯的原始证据（全是推断）")
        if reasons:
            return Extraction(None, "REJECT_INCOMPLETE_EVIDENCE", reasons)

        # ---- 门 3：根因是否经过验证 --------------------------------------
        verified = conclusion.terminal_state is TerminalState.RESOLVED
        status = ExperienceStatus.VERIFIED if verified else ExperienceStatus.CANDIDATE
        reasons.append(
            "恢复后复测验证成功 -> 已验证经验"
            if verified else "仅定位未验证 -> 候选经验（可作为诊断参考，不作为恢复依据）"
        )

        # ---- 门 4：是否与已有案例重复 ------------------------------------
        case = ExperienceCase(
            status=status,
            failure_signature=ctx.signature(),
            symptoms=[
                f"{key}: 预期 {item.get('expected')} 实际 {item.get('actual')}"
                for key, item in list(ctx.expected_actual_diff.items())[:3]
            ] or ["测试结果与预期不一致"],
            hypotheses=[item["key"] for item in conclusion.hypotheses],
            verified_root_cause=VerifiedRootCause(
                key=conclusion.root_cause_key,
                description=conclusion.root_cause or "",
                evidence_ids=supporting,
            ),
            diagnostic_steps=list(conclusion.diagnostic_path),
            recovery_actions=[item.action for item in conclusion.recovery if item.executed],
            verification_result=dict(conclusion.verification),
            applicability={
                "software_versions": [ctx.software_version] if ctx.software_version else [],
                "stages": [ctx.failed_stage] if ctx.failed_stage else [],
                "terminal_state": conclusion.terminal_state.value,
            },
            effective=verified,
        )

        existing_id = self._by_signature.get(case.signature_hash)
        if existing_id is not None:
            existing = self._cases[existing_id]
            # 重复案例：如果新的是 VERIFIED 而旧的是 CANDIDATE，允许升级
            if (existing.status is ExperienceStatus.CANDIDATE
                    and case.status is ExperienceStatus.VERIFIED):
                existing.status = ExperienceStatus.VERIFIED
                existing.effective = True
                existing.recovery_actions = case.recovery_actions
                existing.verification_result = case.verification_result
                reasons.append(f"重复案例 {existing_id} 由候选升级为已验证")
                self.audit.append({"action": "UPGRADE", "case_id": existing_id})
                return Extraction(existing, "PUBLISH_UPGRADED", reasons)
            return Extraction(existing, "REJECT_DUPLICATE",
                              reasons + [f"与已有案例 {existing_id} 指纹相同"])

        # ---- 门 5：发布 ------------------------------------------------
        self._cases[case.case_id] = case
        self._by_signature[case.signature_hash] = case.case_id
        self.audit.append({"action": "PUBLISH", "case_id": case.case_id,
                           "status": case.status.value})
        return Extraction(case, f"PUBLISH_{case.status.value}", reasons)

    # ------------------------------------------------------------------
    def record_failure(
        self,
        ctx: FailureContext,
        *,
        attempted_actions: list[str],
        symptom: str,
    ) -> tuple[ExperienceCase, bool]:
        """记录一条**失败经验**（§8.2 的第三类），返回 ``(案例, 是否新建)``。

        典型来源：某个恢复动作执行了但没效果，甚至把环境弄得更糟。
        这类经验的价值是负向的 —— 它让下一次排障直接跳过已经证明无效的路。

        **必须去重**：失败经验是按「某动作对这个故障无效」记账的，同一组合
        反复出现（例如每次遇到同类故障都要先试一次 refresh）只会把库撑大，
        却不增加任何信息。判据是 ``(指纹, 动作集合)``。
        """
        signature = ctx.signature()
        case = ExperienceCase(
            status=ExperienceStatus.FAILED,
            failure_signature=signature,
            symptoms=[symptom],
            hypotheses=[],
            verified_root_cause=None,
            diagnostic_steps=[],
            recovery_actions=list(attempted_actions),
            verification_result={"effective": False, "reason": symptom},
            applicability={
                "software_versions": [ctx.software_version] if ctx.software_version else [],
                "stages": [ctx.failed_stage] if ctx.failed_stage else [],
            },
            effective=False,
        )
        key = self._failure_key(case)
        existing_id = self._failed_index.get(key)
        if existing_id is not None:
            existing = self._cases[existing_id]
            self.audit.append({"action": "SKIP_DUPLICATE_FAILURE", "case_id": existing_id})
            return existing, False

        self._cases[case.case_id] = case
        self._failed_index[key] = case.case_id
        self.audit.append({"action": "PUBLISH", "case_id": case.case_id,
                           "status": ExperienceStatus.FAILED.value})
        return case, True

    @staticmethod
    def _failure_key(case: ExperienceCase) -> str:
        return stable_hash(
            {
                "signature": case.signature_hash,
                "actions": sorted(case.recovery_actions),
            }
        )

    # ==================================================================
    # 查询
    # ==================================================================
    def cases(self) -> list[ExperienceCase]:
        return list(self._cases.values())

    def by_status(self, status: ExperienceStatus) -> list[ExperienceCase]:
        return [case for case in self._cases.values() if case.status is status]

    def get(self, case_id: str) -> ExperienceCase | None:
        return self._cases.get(case_id)

    def stats(self) -> dict[str, int]:
        return {
            "total": len(self._cases),
            **{
                status.value: len(self.by_status(status))
                for status in ExperienceStatus
            },
        }
