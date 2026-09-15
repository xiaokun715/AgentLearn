"""Result Store —— 结果持久化 + §45 的事务边界。

§44 把两个存储的角色钉死了：

======================  ====================================================
PostgreSQL              **Durable Source of Truth** —— 事实在这里
Redis                   Cache / Coordination —— 只是加速与协调
Object Storage          **大结果本体** —— 只存字节，不存结论
======================  ====================================================

于是「一次成功」在存储层的正确形态是::

    tool_execution   status = SUCCESS, result_id = result_12
    tool_result      (result_12, inline 或 artifact_id)
    execution_event  审计
    outbox_event     待投递给 Redis 的「已成功」通知

**这四件事必须同事务提交**（§45）。反例是说明书里那句最关键的话：

    ✗ Redis = SUCCESS  /  PostgreSQL = PROCESSING
      -> Agent 拿着 SUCCESS 去查结果，**查不到**
      -> 它只能重试，而重试又会命中幂等键的 SUCCESS，再次查不到
      -> 死循环，或者更糟：Agent 以为「结果就是空的」

所以在实现上，本模块**不允许**自己写 `tool_execution`：状态迁移一律通过
:meth:`~app.infra.database.Database.finalize_success` /
:meth:`~app.infra.database.Database.finalize_failure` 完成 ——
它们是「一个事务写多个事实」的唯一入口。把这条约束收进 API，
比写在文档里靠人记住可靠得多。

顺序同样是正确性的一部分（§46）：**先 DB 提交，再由 Outbox 投递器更新 Redis**。
反过来做，进程在两次写之间崩溃就会留下那个撕裂窗口。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from ..config import AppConfig
from ..domain.enums import ExecutionStatus
from ..domain.models import (
    ArtifactRef,
    ExecutionEvent,
    ResultEnvelope,
    ToolResultRecord,
)
from ..infra.database import Database
from ..tools.base import ToolOutput
from .artifact import ArtifactStore, serialize_payload
from .processor import ResultProcessor

logger = logging.getLogger(__name__)


def _sha256(payload: bytes) -> str:
    """内容指纹（§42 ``tool_result.content_hash``）。

    用 sha256 而不是 md5：这里的用途是**审计与去重举证**（「这两次调用的结果
    是不是同一份」），属于对抗性场景，md5 已经不该再用于此。
    （ObjectStore 的 ``etag`` 保留 md5 只是为了对齐 S3 语义，不承担安全职责。）
    """
    return hashlib.sha256(payload).hexdigest()


class ResultStore:
    """结果的读写门面：处理 -> 落库 -> 读回。

    它是 :class:`ResultProcessor`（形态）与 :class:`~app.infra.database.Database`
    （持久化 + 事务）之间的缝合层，职责只有一句：
    **让「结果已持久化」与「执行已成功」永远同时成立。**
    """

    def __init__(self, db: Database, artifacts: ArtifactStore, config: AppConfig) -> None:
        self.db = db
        self.artifacts = artifacts
        self.config = config
        self.processor = ResultProcessor(config, artifacts)

    # ==================================================================
    # 成功路径
    # ==================================================================
    def persist_success(
        self,
        *,
        call_id: str,
        output: ToolOutput,
        events: Optional[list[ExecutionEvent]] = None,
    ) -> tuple[Optional[ToolResultRecord], ResultEnvelope]:
        """处理结果 + 事务化提交「SUCCESS + 结果」。

        顺序是刻意的：**先 process（可能写对象存储），再开 DB 事务**。
        对象存储的写无法与 DB 事务原子化（跨系统两阶段提交的复杂度不值得），
        所以选择「先写不变量、后写快照」的方向：
        极端情况下会留下一个**没人引用的孤儿 artifact**（浪费存储，可被清理任务回收），
        而绝不会出现「DB 说结果在 artifact_xxx，而 artifact_xxx 根本不存在」
        （那会让 Agent 拿到一个取不回来的引用）。

        :returns: ``(tool_result 行, Agent 可见的 envelope)``。
            envelope 必须回给调用方 —— Agent 要的是它，不是 DB 行。
        """
        envelope = self.processor.process(output, call_id=call_id)
        record = self._to_record(call_id, output, envelope)
        stored = self.db.finalize_success(call_id=call_id, result=record, events=events)
        return stored, envelope

    def _to_record(
        self,
        call_id: str,
        output: ToolOutput,
        envelope: ResultEnvelope,
    ) -> ToolResultRecord:
        """把 envelope 折算成 ``tool_result`` 行。

        两条路径的**载荷形态**：

        - ``inline``   -> ``inline_result`` = 结果的 JSON 文本
        - ``artifact`` -> ``artifact_id``    = 对象存储里的键，DB 里**不存大结果本体**

        为什么 inline 也要 JSON 序列化而不是原样存字符串：
        读回时 :meth:`load_envelope` 需要无歧义地还原类型（``"42"`` 与 ``42``
        必须能区分）。代价是二进制小结果会被 ``default=str`` 降级成字符串 ——
        平台约定 inline 只承载 JSON 语义的结果，二进制一律走 artifact。
        """
        if envelope.result_type == "inline":
            # 与 ResultProcessor 的尺寸口径一致：同一个序列化函数
            inline_payload = json.dumps(
                envelope.data, ensure_ascii=False, default=str
            )
            return ToolResultRecord(
                call_id=call_id,
                result_type="inline",
                inline_result=inline_payload,
                size_bytes=envelope.size_bytes,
                content_hash=_sha256(inline_payload.encode("utf-8")),
            )

        assert envelope.artifact is not None, "artifact 类型的结果必须带 ArtifactRef"
        artifact_id = envelope.artifact.artifact_id
        return ToolResultRecord(
            call_id=call_id,
            result_type="artifact",
            artifact_id=artifact_id,
            size_bytes=envelope.size_bytes,
            # 与 ArtifactStore 写入的字节**完全同源**（同一个 serialize_payload），
            # 因此这个 hash 可以与对象存储里的真实内容对账。
            content_hash=_sha256(
                serialize_payload(output.data, content_type=output.content_type)
            ),
        )

    # ==================================================================
    # 失败路径
    # ==================================================================
    def persist_failure(
        self,
        *,
        call_id: str,
        error_type: str,
        error_message: str,
        status: ExecutionStatus = ExecutionStatus.FAILED,
        events: Optional[list[ExecutionEvent]] = None,
    ) -> None:
        """失败终态提交（状态 + 审计 + Outbox 同事务）。

        ``status`` 之所以可传入而不是固定 ``FAILED``：

        - ``CANCELLED`` —— Agent 主动取消（§41 ``PROCESSING -> CANCELLING``）
        - ``RECOVERY_REQUIRED`` —— 租约过期且执行状态不确定（§56），
          此时**不能**记成 FAILED，那会让恢复流程误以为「这里已经结束了」而放弃接管。

        失败路径**不写** ``tool_result``：没有结果就是没有结果，
        写一行空结果会让「查得到结果 = 执行成功」这个简单推论失效。
        """
        self.db.finalize_failure(
            call_id=call_id,
            error_type=error_type,
            error_message=error_message,
            status=status,
            events=events,
        )

    # ==================================================================
    # 读回路径（恢复 / get_artifact / 结果查询接口）
    # ==================================================================
    def load_envelope(self, call_id: str) -> Optional[ResultEnvelope]:
        """从 DB 把结果读回成 Agent 可见的 envelope。

        这是「DB 是唯一事实来源」的检验：**不查 Redis、不查内存缓存**，
        只读 ``tool_result``，凭它重建 envelope。如果这条路径读不出东西，
        那说明 §45 说的那个撕裂真的发生了 —— 而它本不该发生。

        artifact 路径的 preview 是**重新截取**的，不是缓存下来的：
        preview 是 artifact 的纯函数，缓存只会带来「缓存与本体不一致」的风险。
        """
        record = self.db.get_result(call_id)
        if record is None:
            return None

        if record.result_type == "inline":
            data: Any = None
            if record.inline_result is not None:
                try:
                    data = json.loads(record.inline_result)
                except json.JSONDecodeError:
                    # 早期版本可能存了裸字符串；按纯文本容错读回，
                    # 而不是让一个历史格式问题把整条恢复路径打挂。
                    logger.warning("inline_result 非 JSON，按纯文本读回 call=%s", call_id)
                    data = record.inline_result
            return ResultEnvelope(
                result_type="inline",
                status="success",
                data=data,
                size_bytes=record.size_bytes,
                truncated=False,
            )

        assert record.artifact_id, "artifact 类型的结果行必须有 artifact_id"
        # 读一次字节，stat 一次元信息；preview 由这份字节就地重算
        raw = self.artifacts.load(record.artifact_id)
        stat = self.artifacts.stat(record.artifact_id)
        text = raw.decode("utf-8", errors="replace")
        ref = ArtifactRef(
            artifact_id=record.artifact_id,
            size=stat.size,
            preview=ArtifactStore.build_preview(
                text,
                head=self.config.preview_head_size,
                tail=self.config.preview_tail_size,
            ),
            content_type=stat.content_type,
        )
        return ResultEnvelope(
            result_type="artifact",
            status="success",
            data=None,
            artifact=ref,
            size_bytes=record.size_bytes,
            # 能落到 artifact 就说明它超过了 inline 阈值，对 Agent 而言必然是被截断的
            truncated=True,
            stats={
                "original_size_bytes": record.size_bytes,
                "line_count": text.count("\n") + 1,
                "truncated": True,
                "preview_bytes": len(ref.preview.encode("utf-8")),
                "artifact_id": record.artifact_id,
                "inline_limit_bytes": self.config.max_inline_size,
                "content_hash": record.content_hash,
            },
        )

    def get_result(self, call_id: str) -> Optional[ToolResultRecord]:
        """拿原始 ``tool_result`` 行（管理接口 / 对账用）。"""
        return self.db.get_result(call_id)

    def read_artifact(self, artifact_id: str) -> bytes:
        """读 artifact 完整字节 —— ``get_artifact`` 的落地。

        这里故意**不做任何截断**：调用方（get_artifact Tool）既然明确点名要
        artifact，再截断就等于欺骗。它的输出会**再次**经过 Result Processor，
        于是「下载大 artifact 又得到一个 artifact」这件事是被允许且自洽的。
        """
        return self.artifacts.load(artifact_id)

    def artifact_ref(self, artifact_id: str) -> ArtifactRef:
        """按 artifact_id 重建引用（size 取实际对象大小，preview 按 §40 重算）。"""
        stat = self.artifacts.stat(artifact_id)
        preview = self.processor.rebuild_preview(artifact_id)
        return ArtifactRef(
            artifact_id=artifact_id,
            size=stat.size,
            preview=preview,
            content_type=stat.content_type,
        )
