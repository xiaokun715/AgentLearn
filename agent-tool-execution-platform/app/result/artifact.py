"""Artifact 存取 —— 大结果落 Object Storage 的薄封装（说明书 §39 / §40）。

§39 说得很直白：**大结果不进 Context，只给引用。**

    Tool 返回 100MB 日志
        → Object Storage 存完整内容
        → Agent 只拿到 artifact_id + preview(前 8KB + 后 8KB + 统计)
        → 真要看细节，再调 ``get_artifact`` 按需取

本模块只做三件事，**不做尺寸判断**（那是 :class:`~app.result.processor.ResultProcessor`
的职责）：

1. 把任意 Tool 产物序列化成 bytes（JSON / 纯文本 / 二进制）；
2. put 进 :class:`~app.infra.object_store.ObjectStore`，返回
   :class:`~app.domain.models.ArtifactRef`；
3. 按 §40 拼 preview。

刻意保持「薄」：一旦这里开始做截断、压缩、分片，它就会变成第二个 Result Processor，
两条路径迟早会不一致 —— 而 §39/§40 的一致性正是「Agent 看到的 preview 与
真实 artifact 是同一份数据」这件事的全部依据。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..domain.models import ArtifactRef, new_artifact_id
from ..infra.object_store import ObjectStat, ObjectStore

logger = logging.getLogger(__name__)


# ======================================================================
# 序列化原语（ResultProcessor / ResultStore 共用，避免三条路径三种编码）
# ======================================================================
def serialize_payload(data: Any, *, content_type: str = "application/json") -> bytes:
    """把 Tool 产物序列化成**确定性** bytes。

    确定性很重要：:class:`~app.result.store.ResultStore` 用同一函数算
    ``content_hash``，如果两边各写一套，hash 与真实字节就会对不上，
    事后审计「这份 artifact 有没有被篡改」就失去意义。

    规则（与 :meth:`app.tools.base.ToolOutput.size_bytes` 保持一致的口径）：

    - ``bytes`` / ``bytearray`` -> 原样（二进制结果不该被二次编码）
    - ``str`` -> UTF-8 直接编码（纯文本结果不该被加上 JSON 引号）
    - 其它 -> ``json.dumps(ensure_ascii=False, default=str)``
      （``default=str`` 让 datetime / Decimal / 自定义对象也能落盘，而不是抛错 ——
      §39 的诉求是「先完整存下来」，而不是「先严格要求可序列化」）
    """
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8")
    return json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")


def payload_as_text(data: Any) -> str:
    """把 Tool 产物转成**给人看**的文本，供 preview 截断使用（§40）。

    与 :func:`serialize_payload` 分开，是因为 preview 的输入是「文本本身」，
    而 artifact 的输入是「字节」：二进制结果没有「前 8KB 文本」可言，
    此时退回 ``repr`` 至少能让 Agent 看出这是二进制。
    """
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", errors="replace")
    return json.dumps(data, ensure_ascii=False, default=str)


class ArtifactStore:
    """artifact 的读写门面（§39）。

    为什么 ``artifact_id`` 不做 ``call_id`` 前缀命名空间：
    Agent 手上**只有** ``artifact_id``（:class:`~app.domain.models.ArtifactRef`
    里就这一个标识），``load`` / ``stat`` 都无法反推它属于哪次 call。
    ``new_artifact_id()`` 生成的 id 本身全局唯一（uuid 片段），
    按 call 分目录只会让「拿 id 取不回数据」这个坑出现在每一次 ``get_artifact``。
    需要按 call 清理时，走 ``tool_result`` 表反查即可。
    """

    def __init__(self, object_store: ObjectStore, *, clock: Any = None) -> None:
        self.object_store = object_store
        self.clock = clock

    # ------------------------------------------------------------------
    def _now(self) -> float:
        """统一时间源（与 §14 租约、§36 退避同一套 Clock，便于演示拨快时间）。"""
        return self.clock.time() if self.clock is not None else time.time()

    # ------------------------------------------------------------------
    def save(
        self,
        data: Any,
        *,
        call_id: str,
        content_type: str = "application/json",
        preview: str = "",
    ) -> ArtifactRef:
        """把完整结果落对象存储，返回给 Agent 的引用（§39）。

        :param call_id: 仅用于日志/审计定位。**不参与对象键** —— 见类 docstring。
        :param preview: 由 :class:`~app.result.processor.ResultProcessor` 按 §40
            生成；这里只做「透传」，不自己截断（职责边界）。
        """
        artifact_id = new_artifact_id()
        payload = serialize_payload(data, content_type=content_type)
        stat = self.object_store.put(
            artifact_id, payload, content_type=content_type
        )
        logger.debug(
            "artifact saved id=%s call=%s size=%d at=%.3f",
            artifact_id,
            call_id,
            stat.size,
            self._now(),
        )
        return ArtifactRef(
            artifact_id=artifact_id,
            size=stat.size,
            preview=preview,
            content_type=content_type,
        )

    def load(self, artifact_id: str) -> bytes:
        """取回完整内容（``get_artifact`` Tool 的实现基础）。"""
        return self.object_store.get(artifact_id)

    def load_json(self, artifact_id: str) -> Any:
        """取回并反序列化 —— 大结果绝大多数是 JSON 结构。"""
        return json.loads(self.load(artifact_id).decode("utf-8"))

    def stat(self, artifact_id: str) -> ObjectStat:
        return self.object_store.stat(artifact_id)

    def exists(self, artifact_id: str) -> bool:
        return self.object_store.exists(artifact_id)

    def delete(self, artifact_id: str) -> None:
        """删除 artifact（保留策略 / 人工清理用）。重复删除必须无害。"""
        self.object_store.delete(artifact_id)

    # ------------------------------------------------------------------
    @staticmethod
    def build_preview(data: str, *, head: int, tail: int) -> str:
        """按 §40 生成预览：**前 8KB + 后 8KB + 统计信息**。

        ::

            result preview = 前 head 字节
                           + "\\n...<省略 N 字节>...\\n"
                           + 后 tail 字节
                           + 统计信息（总字节数 / 行数 / 省略量）

        为什么头尾都要：头让人看清「这是什么」，尾藏着**最有价值的那一行** ——
        堆栈的最后一个 ``Caused by``、测试报告末尾的 ``5 failed, 10 passed``、
        日志最后的退出码。只留头部会把结论切掉，Agent 于是只能 Download 整个
        artifact（把 §40 想省的 token 又花回去了）。

        为什么短数据原样返回：给一个比原文还长的「预览」毫无意义，
        反而会让 Agent 以为结果被截断了。

        为什么按**字节**切而不是按字符切：``head`` / ``tail`` 的语义就是字节
        （§40 说的是 8KB）。中文一个字 3 字节，若按字符切，8192 个字符会
        产出 24KB 的预览 —— 整整 3 倍于预算，而「预览不该超过 16KB」正是
        §40 想立的那条规矩。切完再用 ``errors="ignore"`` 解码，
        保证不会把一个多字节字符劈成半个（看到乱码比看到截断更糟）。
        """
        raw = data.encode("utf-8")
        total_bytes = len(raw)
        if total_bytes <= head + tail:
            return data

        # 短于 head+tail 的判定用字节；相等时也原样返回，避免「预览比原文还长」
        omitted = total_bytes - head - tail
        line_count = data.count("\n") + 1
        head_text = raw[:head].decode("utf-8", errors="ignore")
        tail_text = raw[-tail:].decode("utf-8", errors="ignore")
        return (
            f"{head_text}\n"
            f"...<省略 {omitted} 字节>...\n"
            f"{tail_text}\n"
            f"\n[统计] 总长度={total_bytes} 字节 / 总行数={line_count} 行 / "
            f"已省略={omitted} 字节 / 预览={head}+{tail} 字节"
        )

    # ------------------------------------------------------------------
    def preview_for(
        self,
        artifact_id: str,
        *,
        head: int,
        tail: int,
    ) -> str:
        """从**已落盘**的 artifact 重新截取 preview。

        §45 的「DB 是唯一事实来源」推论：``tool_result`` 行里没存 preview
        （它可以从 artifact 重算），于是恢复路径 / ``load_envelope`` 必须能重建它。
        重算而不是缓存，是为了避免「缓存里的 preview 与 artifact 内容不一致」
        这种最难查的脏数据。
        """
        text = self.load(artifact_id).decode("utf-8", errors="replace")
        return self.build_preview(text, head=head, tail=tail)
