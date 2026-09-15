"""LargeFileAnalysisTool —— §60 的**大结果 / artifact** 主力（说明书 §39 / §40 / §60 / §71 Test 5）。

§60 矩阵第五行：

    ========================  ========  ==========  ==================================
    Tool                      模式      耗时        被用来验证什么
    ========================  ========  ==========  ==================================
    large_file_analysis       async     45s         大结果 + 截断 + Object Storage
    ========================  ========  ==========  ==================================

它是 §39/§40 的**唯一触发源**：没有大结果，Result Processor 里那条 artifact
分支就永远走不到，于是「结果太大不进 Context」这条设计在整个系统里从未被验证过。

**§71 Test 5 的场景**：分析一个大文件 -> 结果远超 ``max_inline_size``(32KB)
-> 平台把完整结果落对象存储 -> Agent 只拿到 ``artifact_id`` + 前 8KB/后 8KB 预览
-> Agent 判断需要细节时再调 ``get_artifact``。

两种模式
--------

1. **真实文件**（``path`` 存在）：按 ``chunk_size`` 分块读，统计字节数、行数、
   每块的摘要与词频 top 20。真读，不做假。
2. **合成数据**（``path`` 不存在但给了 ``size_mb``）：不落盘，直接生成结构化结果，
   并在 ``stats`` 与返回值里标 ``synthetic=True``。**演示必须能「一定能拿到大结果」**，
   否则 §39 那条路径是否被走到就取决于环境里恰好有没有大文件 —— 这种演示不可靠。

``path`` 不存在且**没有** ``size_mb`` 时，按 ``_DEFAULT_SYNTHETIC_MB`` 合成：
保证「随手一调就能看到 artifact 路径」。
"""
from __future__ import annotations

import os
import re
from collections import Counter
from typing import Any, ClassVar, Optional, Type

from pydantic import BaseModel, Field

from ..domain.enums import ExecutionMode, IdempotencyLevel, RiskLevel
from ..domain.errors import BusinessError
from ..domain.models import ToolMetadata
from .base import BaseTool, ToolContext, ToolOutput

_DEFAULT_SYNTHETIC_MB = 8.0
"""默认合成体积。选 8MB 而不是「刚好超过 32KB」：§40 的演示要同时展示
「原始大小远超阈值」和「预览被压缩到 16KB」的对比效果。"""

_PREVIEW_CHARS = 1024
"""每个 chunk 记录的摘要长度。128 个 chunk × ~1.1KB ≈ 140KB > 100KB ——
默认参数下必然突破 ``max_inline_size``，artifact 路径**一定**会被走到。"""

_MAX_CHUNK_RECORDS = 512
"""chunks 列表的封顶。Tool 自己也不该在内存里造出 GB 级中间结果：
它只是个分析器，不是打包器。超出部分仍计入 size/line/词频统计，
只是不再逐块留明细。"""

_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}|[一-鿿]{2,}")

_SYNTHETIC_TERMS = [
    "lease", "idempotency", "checkpoint", "sandbox", "artifact",
    "heartbeat", "outbox", "retry", "backoff", "reaper",
    "worker", "tenant", "envelope", "preview", "truncate",
    "dedup", "timeout", "recovery", "approval", "quota",
]


class LargeFileAnalysisArgs(BaseModel):
    """参数模型 —— 与 ``configs/tools.yaml`` 的 ``large_file_analysis.params`` 对齐。"""

    path: str = Field(
        min_length=1,
        max_length=1024,
        description="待分析文件路径；文件不存在时按 size_mb 合成数据集",
    )
    chunk_size: int = Field(
        default=65536, ge=1024, le=1048576, description="分块大小（字节）"
    )
    size_mb: Optional[float] = Field(
        default=None, gt=0, le=2048, description="合成数据集大小（MB），仅当文件不存在时生效"
    )


class LargeFileAnalysisTool(BaseTool):
    """大文件分块分析（async / estimated 45s / risk=low / idempotency=idempotent）。"""

    name: ClassVar[str] = "large_file_analysis"
    category: ClassVar[str] = "async.artifact"
    args_model: ClassVar[Type[BaseModel]] = LargeFileAnalysisArgs

    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        name="large_file_analysis",
        version="1.0",
        description=(
            "对大文件做分块统计：总字节数、行数、每个分块的摘要、全局词频 top 20。"
            "结果通常超过 32KB，会由结果处理器自动转成 artifact 引用加预览。"
        ),
        execution_mode=ExecutionMode.ASYNC,
        estimated_duration_ms=45_000,
        timeout_ms=300_000,
        cpu_limit=2.0,
        memory_limit_mb=2048,
        network_access=False,
        idempotent=True,
        risk_level=RiskLevel.LOW,
        required_permissions=["file.read"],
        idempotency_level=IdempotencyLevel.IDEMPOTENT,
    )

    # ------------------------------------------------------------------
    def run(self, args: LargeFileAnalysisArgs, ctx: ToolContext) -> ToolOutput:
        """按模式分派：真实文件 or 合成数据。"""
        if os.path.exists(args.path):
            if os.path.isdir(args.path):
                # 传目录是个明确的调用错误：本 Tool 分析的是文件。
                raise BusinessError(
                    f"path 指向目录而非文件: {args.path}",
                    detail={"path": args.path},
                )
            return self._analyse_real_file(args, ctx)
        return self._analyse_synthetic(args, ctx)

    # ==================================================================
    # 模式一：真实文件
    # ==================================================================
    def _analyse_real_file(
        self, args: LargeFileAnalysisArgs, ctx: ToolContext
    ) -> ToolOutput:
        """真读、真统计。分块读取，**不把整个文件读进内存**。

        大文件分析 Tool 自己 OOM 是最讽刺的失败方式（§27 Resource Exhausted），
        所以这里严格按 ``chunk_size`` 流式推进。
        """
        size_bytes = os.path.getsize(args.path)
        line_count = 0
        chunks: list[dict[str, Any]] = []
        counter: Counter[str] = Counter()
        offset = 0
        index = 0

        with open(args.path, "rb") as handle:
            while True:
                raw = handle.read(args.chunk_size)
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace")
                line_count += raw.count(b"\n")
                counter.update(_TERM_RE.findall(text))

                if len(chunks) < _MAX_CHUNK_RECORDS:
                    chunks.append(
                        {
                            "index": index,
                            "offset": offset,
                            "length": len(raw),
                            "preview": text[:_PREVIEW_CHARS],
                        }
                    )
                offset += len(raw)
                index += 1
                # §50：按字节进度上报。分块循环是天然的进度点，不用它太浪费。
                if size_bytes:
                    ctx.emit_progress(
                        min(99.0, offset / size_bytes * 100.0),
                        f"已分析 {offset}/{size_bytes} 字节（{index} 块）",
                    )

        if size_bytes:
            line_count += 1  # 最后一行通常没有换行符

        return ToolOutput(
            data={
                "path": args.path,
                "size_bytes": size_bytes,
                "line_count": line_count,
                "chunks": chunks,
                "top_terms": [[term, count] for term, count in counter.most_common(20)],
                "synthetic": False,
            },
            stats={
                "mode": "real_file",
                "chunk_size": args.chunk_size,
                "chunk_total": index,
                "chunk_records": len(chunks),
                "chunk_records_capped": index > len(chunks),
                "duration_ms": ctx.elapsed_ms(),
            },
        )

    # ==================================================================
    # 模式二：合成数据集
    # ==================================================================
    def _analyse_synthetic(
        self, args: LargeFileAnalysisArgs, ctx: ToolContext
    ) -> ToolOutput:
        """生成一份**确定性的**大结果（不落盘，直接产出结构化数据）。

        「确定性」是刻意的：同一个 size_mb 每次得到同样的 chunk 与词频，
        于是「结果被 artifact 化了」这件事可以在两次演示之间被逐字节比对。
        """
        size_mb = args.size_mb if args.size_mb is not None else _DEFAULT_SYNTHETIC_MB
        total_bytes = int(size_mb * 1024 * 1024)
        chunk_size = args.chunk_size
        chunk_total = max(1, (total_bytes + chunk_size - 1) // chunk_size)
        record_count = min(chunk_total, _MAX_CHUNK_RECORDS)

        chunks: list[dict[str, Any]] = []
        counter: Counter[str] = Counter()
        for index in range(record_count):
            offset = index * chunk_size
            length = min(chunk_size, total_bytes - offset)
            text = self._synthetic_text(index, min(length, _PREVIEW_CHARS))
            terms = _TERM_RE.findall(text)
            counter.update(terms)
            chunks.append(
                {
                    "index": index,
                    "offset": offset,
                    "length": length,
                    "preview": text,
                    # 每块自己的词频 top5：让结果结构更真实（真实分析器也会这么给），
                    # 顺带把结果体积稳稳推过 artifact 阈值。
                    "top_terms": Counter(terms).most_common(5),
                }
            )
            ctx.emit_progress(
                (index + 1) / record_count * 100.0,
                f"已合成 {index + 1}/{record_count} 块",
            )

        # 合成数据的「行数」按每行约 80 字符估算 —— 生成器就是这么造的，
        # 统计值与数据本身自洽，不会出现「8MB 却只有 3 行」的荒谬数字。
        line_count = max(1, total_bytes // 80)

        return ToolOutput(
            data={
                "path": args.path,
                "size_bytes": total_bytes,
                "line_count": line_count,
                "chunks": chunks,
                "top_terms": [[term, count] for term, count in counter.most_common(20)],
                # 明示身份：合成结果**绝不能**被误读成对真实文件的分析结论
                "synthetic": True,
            },
            stats={
                "mode": "synthetic",
                "synthetic": True,
                "chunk_size": chunk_size,
                "chunk_total": chunk_total,
                "chunk_records": record_count,
                "chunk_records_capped": chunk_total > record_count,
                "note": (
                    "path 不存在，按 size_mb 生成数据集"
                    if args.size_mb is not None
                    else f"path 不存在且未给 size_mb，使用默认 {_DEFAULT_SYNTHETIC_MB}MB"
                ),
                "duration_ms": ctx.elapsed_ms(),
            },
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _synthetic_text(index: int, length: int) -> str:
        """确定性伪文本：术语按 index 步进轮转，凑够 ``length`` 个字符。

        步长 7 与术语数 20 互质，于是每个 chunk 从不同的相位开始轮转 ——
        块与块之间不重复，但**同一个 index 永远得到同一段文本**（可复现）。
        """
        need = length // 6 + 2
        words = [
            _SYNTHETIC_TERMS[(index + i * 7) % len(_SYNTHETIC_TERMS)]
            for i in range(need)
        ]
        return " ".join(words)[:length]
