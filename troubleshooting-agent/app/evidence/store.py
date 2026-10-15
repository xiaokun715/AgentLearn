"""证据库 —— 管理证据引用、来源与版本（说明书 §10.2）。

这个模块的职责不是「存日志」，而是**维持证据的层级秩序**。说明书 §3.1 的原话：

> 不能把模型生成的「设备可能异常」直接存成事实。

所以这里把「写入」拆成三个语义不同的入口，而不是一个万能 ``add()``：

- :meth:`EvidenceStore.record_raw` —— 原始日志 / 接口原文；
- :meth:`EvidenceStore.record_observed` —— 观测事实（工具给出的确定性结论）；
- :meth:`EvidenceStore.infer` —— 模型推断，**必须**指向它依据的原始证据。

第三条的强制约束由 :class:`~app.domain.models.Evidence` 的校验器兜底，
这里只负责把「谁推的、基于哪些证据」串起来。
"""
from __future__ import annotations

from datetime import datetime

from ..domain.enums import EvidenceKind
from ..domain.models import Evidence, EvidenceDraft, utcnow


class EvidenceStore:
    """进程内证据库。

    真机上应当是「对象存储放正文 + 数据库放元数据」，本 Demo 用同进程字典等价替代：
    接口语义（引用、分层、溯源）是一致的，换存储不影响上层。
    """

    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}
        self._order: list[str] = []
        #: ``evidence_id -> 入库序号``。排序一律用它，**不用 ``collected_at``** ——
        #: Windows 上 ``datetime.now()`` 的粒度约 15ms，同一批证据会拿到完全相同
        #: 的时间戳，按时间排序就退化成「看运气」。
        self._index: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def record_raw(
        self,
        *,
        source: str,
        claim: str,
        payload: dict | None = None,
        ref: str | None = None,
    ) -> Evidence:
        """原始证据：日志原文、接口原始返回。``ref`` 指向对象存储里的正文。"""
        evidence = Evidence(
            kind=EvidenceKind.RAW,
            source=source,
            claim=claim,
            payload=payload or {},
            ref=ref,
        )
        return self._put(evidence)

    def record_observed(
        self,
        *,
        source: str,
        claim: str,
        payload: dict | None = None,
        derived_from: list[str] | None = None,
    ) -> Evidence:
        """观测事实：工具对原始证据做出的确定性判断（如「设备不可用」）。

        允许带 ``derived_from``（如果它是从原始证据里读出来的），但不强制 ——
        环境探针这类直接读设备状态的检查，本身就没有上游原始证据。
        """
        evidence = Evidence(
            kind=EvidenceKind.OBSERVED,
            source=source,
            claim=claim,
            payload=payload or {},
            derived_from=derived_from or [],
        )
        return self._put(evidence)

    def infer(
        self,
        *,
        source: str,
        claim: str,
        derived_from: list[str],
        payload: dict | None = None,
    ) -> Evidence:
        """模型推断：**必须**给出依据。没有依据的推断在这里就写不进来。"""
        evidence = Evidence(
            kind=EvidenceKind.INFERRED,
            source=source,
            claim=claim,
            payload=payload or {},
            derived_from=list(derived_from),
        )
        return self._put(evidence)

    def verify(
        self,
        *,
        source: str,
        claim: str,
        derived_from: list[str],
        payload: dict | None = None,
    ) -> Evidence:
        """验证结论：复测/对照实验的结论（如「环境恢复后复测通过」）。"""
        evidence = Evidence(
            kind=EvidenceKind.VERIFIED,
            source=source,
            claim=claim,
            payload=payload or {},
            derived_from=list(derived_from),
        )
        return self._put(evidence)

    def materialize(
        self,
        drafts: list[EvidenceDraft],
        *,
        source: str,
        derived_from: list[str] | None = None,
    ) -> list[Evidence]:
        """把工具交上来的证据草稿入库。

        分两趟走是**故意的**：先落 RAW / OBSERVED 拿到 ID，再落 INFERRED / VERIFIED，
        并把前一趟的 ID 作为它们的 ``derived_from``。这样「推断必须指向原始证据」
        这条约束不是靠调用方自觉 —— 证据库自己就把指针接上了。
        """
        _Kind = EvidenceKind

        if not drafts:
            return []

        upstream = list(derived_from or [])
        produced: list[Evidence] = []

        def _drain(kind: _Kind, anchors: list[str]) -> None:
            for draft in drafts:
                if draft.kind is not kind:
                    continue
                if kind is _Kind.RAW:
                    produced.append(self.record_raw(source=source, claim=draft.claim,
                                                   payload=draft.payload, ref=draft.ref))
                elif kind is _Kind.OBSERVED:
                    produced.append(self.record_observed(source=source, claim=draft.claim,
                                                         payload=draft.payload,
                                                         derived_from=anchors))
                elif kind is _Kind.INFERRED:
                    produced.append(self.infer(source=source, claim=draft.claim,
                                               derived_from=anchors, payload=draft.payload))
                else:
                    produced.append(self.verify(source=source, claim=draft.claim,
                                                derived_from=anchors, payload=draft.payload))

        # 四趟：RAW → OBSERVED → INFERRED → VERIFIED，逐层加深。
        # 每一趟都能引用上一趟产出的 ID，所以「推断必须指回原始证据」
        # 是结构性成立的，而不是靠工具作者自觉。
        for kind in (_Kind.RAW, _Kind.OBSERVED, _Kind.INFERRED, _Kind.VERIFIED):
            anchors = upstream + [
                item.evidence_id for item in produced if item.evidence_id not in upstream
            ]
            _drain(kind, anchors)
        return produced

    def _put(self, evidence: Evidence) -> Evidence:
        self._items[evidence.evidence_id] = evidence
        self._order.append(evidence.evidence_id)
        self._index[evidence.evidence_id] = len(self._order)
        return evidence

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)

    def many(self, evidence_ids: list[str]) -> list[Evidence]:
        return [self._items[eid] for eid in evidence_ids if eid in self._items]

    def all(self) -> list[Evidence]:
        return [self._items[eid] for eid in self._order]

    def by_kind(self, kind: EvidenceKind) -> list[Evidence]:
        return [e for e in self.all() if e.kind is kind]

    def by_source(self, source: str) -> list[Evidence]:
        return [e for e in self.all() if e.source == source]

    def since(self, moment: datetime) -> list[Evidence]:
        return [e for e in self.all() if e.collected_at >= moment]

    # ------------------------------------------------------------------
    # 溯源
    # ------------------------------------------------------------------
    def lineage(self, evidence_id: str) -> list[str]:
        """把一条证据的全部上游原始证据摊平（用于结论里的证据回溯）。"""
        seen: list[str] = []
        stack = [evidence_id]
        while stack:
            current = stack.pop()
            item = self._items.get(current)
            if item is None or current in seen:
                continue
            seen.append(current)
            stack.extend(item.derived_from)
        # 稳定的展示顺序：按入库先后 —— 因为 RAW 一定先于由它推出的 OBSERVED/INFERRED
        # 入库，所以这个顺序天然就是「先原始后推断」，而且完全可复现。
        seen.sort(key=lambda eid: self._index.get(eid, 0))
        return seen

    def raw_ancestors(self, evidence_ids: list[str]) -> list[str]:
        """一组证据背后的**原始证据**集合。

        结论里只引用原始证据，是「不让推断变成事实」的最后一道闸门：
        报告读者顺着引用点下去，看到的一定是日志或接口原文。
        """
        result: list[str] = []
        for eid in evidence_ids:
            for ancestor in self.lineage(eid):
                item = self._items[ancestor]
                if item.is_raw and ancestor not in result:
                    result.append(ancestor)
        return result

    def describe(self, evidence_ids: list[str], limit: int = 4) -> list[str]:
        """把证据 ID 列表转成短描述，供终端展示。"""
        lines: list[str] = []
        for eid in evidence_ids[:limit]:
            item = self._items.get(eid)
            if item:
                lines.append(f"[{item.kind.value}] {item.claim}")
        if len(evidence_ids) > limit:
            lines.append(f"…另有 {len(evidence_ids) - limit} 条")
        return lines

    def snapshot(self) -> dict:
        """恢复前的状态快照（§7.2 要求保留恢复前后快照）。"""
        return {
            "evidence_count": len(self._items),
            "kinds": {
                kind.value: len(self.by_kind(kind))
                for kind in EvidenceKind
            },
            "at": utcnow().isoformat(timespec="seconds"),
        }

    def __len__(self) -> int:
        return len(self._items)
