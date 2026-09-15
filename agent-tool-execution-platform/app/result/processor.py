"""Result Processor —— §37 那张图的落点（说明书 §37 / §38 / §39 / §40）。

§37 的原话意思是:

    **Tool Result = 100MB，不可能 100MB -> LLM Context。**

    所以 Tool 的返回值**不是**直接给 Agent 的东西。中间必须有一层做尺寸判断：
    小的 inline 进 Context，大的落 Object Storage 只给引用 + 预览。

这一层的输入是 :class:`~app.tools.base.ToolOutput`（Tool 的原始产物），
输出是 :class:`~app.domain.models.ResultEnvelope`（Agent 真正看到的东西）::

    ┌──────────────┐   size <= max_inline_size(32KB)   ┌────────────────────┐
    │  ToolOutput  │ ────────────────────────────────► │ ResultEnvelope     │
    │  (原始产物)   │                                   │  result_type=inline│
    │              │   size >  max_inline_size         │  data=<完整结果>    │
    │              │ ────────────────────────────────► ├────────────────────┤
    └──────────────┘                                   │  result_type=       │
                                                       │    artifact        │
                                                       │  artifact=<ref>    │
                                                       │  truncated=True    │
                                                       └────────────────────┘

**为什么阈值是 32KB 而不是「按 token 估算」**：token 数取决于模型与分词器，
把「多大算大」这件事绑到具体模型上，会让同一个 artifact 在不同模型下走不同路径 ——
于是「同一份结果，换模型后行为变了」这种 bug 无法复现。32KB 是确定性的、
可审计的、与模型无关的硬边界（§40）。

**为什么大结果仍然给 preview**：如果只给 ``artifact_id``，Agent 为了判断
「要不要看细节」就必须先下载 —— 于是它每次都会下载，§40 想省的 token 又花回去了。
preview 是「不下载也能做决定」的最小信息量。
"""
from __future__ import annotations

import logging

from ..config import AppConfig
from ..domain.models import ResultEnvelope
from ..tools.base import ToolOutput
from .artifact import ArtifactStore, payload_as_text

logger = logging.getLogger(__name__)


class ResultProcessor:
    """尺寸判断 + 截断 + artifact 化（§37-40）。

    刻意是**无状态**的：它只依赖 ``config`` 与 :class:`ArtifactStore`，
    因此同一份 ToolOutput 在任何时候处理都会得到同样的 envelope。
    持久化（§44/§45）由 :class:`~app.result.store.ResultStore` 负责，
    两者分开是为了让「结果长什么样」与「结果怎么落库」可以独立演化。
    """

    def __init__(self, config: AppConfig, artifacts: ArtifactStore) -> None:
        self.config = config
        self.artifacts = artifacts

    # ------------------------------------------------------------------
    def process(self, output: ToolOutput, *, call_id: str) -> ResultEnvelope:
        """把 Tool 原始产物折算成 Agent 可见的结果信封。

        :param call_id: 只用于日志与 artifact 的归属审计，不参与尺寸判断。
        """
        size = output.size_bytes()

        # ---- §38 小结果：直接 inline ----
        #
        # 「小」不看内容类型、不看 Tool 名字，只看体积 —— 规则越简单，
        # 越不可能在某个 Tool 上出现「明明很小却走了 artifact」的意外。
        if size <= self.config.max_inline_size:
            return ResultEnvelope(
                result_type="inline",
                status="success",
                data=output.data,
                size_bytes=size,
                truncated=False,
                stats=dict(output.stats or {}),
            )

        # ---- §39 / §40 大结果：落盘 + 只回引用 ----
        text = payload_as_text(output.data)
        preview = ArtifactStore.build_preview(
            text,
            head=self.config.preview_head_size,
            tail=self.config.preview_tail_size,
        )
        ref = self.artifacts.save(
            output.data,
            call_id=call_id,
            content_type=output.content_type,
            preview=preview,
        )

        stats = {
            # Tool 自己的统计先铺底（duration_ms / synthetic ...），
            # 平台统计随后覆盖 —— 尺寸类事实必须由平台说了算，Tool 无权改写。
            **dict(output.stats or {}),
            "original_size_bytes": size,
            "line_count": text.count("\n") + 1,
            "truncated": True,
            "preview_bytes": len(preview.encode("utf-8")),
            "artifact_id": ref.artifact_id,
            "inline_limit_bytes": self.config.max_inline_size,
        }
        logger.info(
            "result artifact 化 call=%s size=%dB preview=%dB artifact=%s",
            call_id,
            size,
            len(preview),
            ref.artifact_id,
        )
        return ResultEnvelope(
            result_type="artifact",
            status="success",
            data=None,
            artifact=ref,
            size_bytes=size,
            truncated=True,
            stats=stats,
        )

    # ------------------------------------------------------------------
    def is_inline(self, size_bytes: int) -> bool:
        """尺寸判断的**唯一**实现 —— 供 Gateway / 测试复用，避免阈值被复制粘贴后漂移。"""
        return size_bytes <= self.config.max_inline_size

    def rebuild_preview(self, artifact_id: str) -> str:
        """从对象存储重算 preview（恢复路径用，§45）。"""
        return self.artifacts.preview_for(
            artifact_id,
            head=self.config.preview_head_size,
            tail=self.config.preview_tail_size,
        )
