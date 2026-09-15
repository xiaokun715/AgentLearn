"""SearchKnowledgeTool —— §60 的**同步长耗时 + 可降级** Tool（说明书 §33 / §60 / §66）。

在 §60 能力矩阵里它覆盖第二行：

    ====================  ========  ==========  ==================================
    Tool                  模式      耗时        被用来验证什么
    ====================  ========  ==========  ==================================
    search_knowledge      sync      1~3s        同步 Tool、参数降级、循环检测
    ====================  ========  ==========  ==================================

它存在的两个理由：

1. **§33 降级策略的载体**。同步 Tool 里最容易被「降级」的就是检索：
   超时/资源紧张时，把 ``semantic``（贵、准）降成 ``keyword``（便宜、糙），
   并把 ``top_k`` 调小。本 Tool 支持外部通过 ``arguments["degraded"]``
   傳入降级提示 —— 平台改不了 Tool 的代码，但可以改它的输入。
2. **两种模式必须给出不同结果**。如果 ``semantic`` 与 ``keyword`` 结果一样，
   降级就只是「多花一次调用」的表演，§33 的策略也就无法被验证。
   这里的选择是：**semantic 用字符 bigram 相似度 + 标签加权（模糊），
   keyword 用精确子串命中（严格）** —— 查询 "租约过期" 时前者会召回
   「心跳超时」这类语义邻居，后者只认字面出现过的文档。

``top_k`` 在 YAML 里标了 ``repair_forbidden: true``：降级**只能调小不能调大**，
所以这里的降级逻辑只做 ``min()``，绝不放大（否则就变成「自愈式越权」§23）。
"""
from __future__ import annotations

import re
from typing import ClassVar, Literal, Type

from pydantic import BaseModel, Field

from ..domain.enums import ExecutionMode, IdempotencyLevel, RiskLevel
from ..domain.models import ToolMetadata
from .base import BaseTool, ToolContext, ToolOutput


class SearchArgs(BaseModel):
    """参数模型 —— 与 ``configs/tools.yaml`` 的 ``search_knowledge.params`` 对齐。"""

    query: str = Field(
        min_length=1, max_length=400, description="检索关键词或自然语言问题"
    )
    top_k: int = Field(
        default=5, ge=1, le=20, description="返回条数；降级时只允许被调小"
    )
    mode: Literal["semantic", "keyword"] = Field(
        default="semantic",
        description="semantic=模糊语义召回（字符 bigram + 标签加权）；keyword=精确子串命中",
    )


class KnowledgeDoc(BaseModel):
    """知识库条目（Demo 用：内置在代码里，不依赖外部向量库）。"""

    doc_id: str
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)


# ======================================================================
# 内置知识库：12+ 条，全部围绕「可靠 Tool 执行系统」本身
#
# 为什么内容要跟平台自己的主题相关：§66 的演示里，Agent 用自然语言问
# 「租约过期了怎么办」，检索结果直接就是本系统的设计说明 ——
# 演示中不需要额外编故事，检索结果本身就有说服力。
# ======================================================================
_KNOWLEDGE_BASE: list[KnowledgeDoc] = [
    KnowledgeDoc(
        doc_id="DOC-001",
        title="幂等键的五段式构造",
        content=(
            "Idempotency Key 由 tenant_id + workflow_run_id + logical_step_id + "
            "tool_name + normalized_arguments 五段拼成。不能只 hash 参数："
            "两个不同任务可能恰好参数相同，只 hash 参数会让第二个调用者拿到第一个的结果。"
            "必须带 logical_step_id：同一个 run 里循环调用同一个 Tool 是合法意图，"
            "要靠「逻辑步骤」把「同一步骤被重复提交」和「不同步骤各调一次」区分开。"
        ),
        tags=["幂等", "idempotency", "幂等键"],
    ),
    KnowledgeDoc(
        doc_id="DOC-002",
        title="幂等状态机 PROCESSING / SUCCESS / FAILED",
        content=(
            "Redis 里的 idempotency:{key} 只有三个状态：PROCESSING、SUCCESS、FAILED。"
            "原子创建用 SET key value NX EX 实现，失败说明已有执行者在跑。"
            "命中 PROCESSING 且租约有效 -> Agent 应当 WAIT；"
            "命中 PROCESSING 但租约已过期 -> 进入接管流程；"
            "命中 SUCCESS -> 直接复用已有结果，不再执行。"
        ),
        tags=["幂等", "状态机", "redis"],
    ),
    KnowledgeDoc(
        doc_id="DOC-003",
        title="租约与心跳：Lease TTL 30s / Heartbeat 10s",
        content=(
            "租约证明「这个 call 现在有主」。TTL 取 30 秒、心跳间隔取 10 秒，"
            "于是允许连续丢两次心跳才判死 —— 网络抖一下不至于让执行者被误判为崩溃。"
            "心跳续租必须用 compare-and-renew 检查 lease_id 是否仍是自己，"
            "否则会出现「已被接管，原持有者又续上租约」的双主写入。"
        ),
        tags=["租约", "lease", "心跳", "heartbeat"],
    ),
    KnowledgeDoc(
        doc_id="DOC-004",
        title="租约过期的歧义：RECOVERY_REQUIRED",
        content=(
            "租约过期只说明「没人续租了」，不说明「执行没发生」。"
            "如果一个高风险且非幂等的操作在租约过期时状态不确定，"
            "不能自动重跑，也不能记成 FAILED，而要进入 RECOVERY_REQUIRED，"
            "由恢复策略结合幂等性等级与风险等级决定：可重试的接管重跑，"
            "不确定的高风险操作转人工确认。"
        ),
        tags=["租约", "恢复", "recovery", "接管"],
    ),
    KnowledgeDoc(
        doc_id="DOC-005",
        title="沙箱资源限制：CPU / 内存 / 磁盘 / 网络 / 进程数",
        content=(
            "沙箱按 CPU 配额、内存上限、磁盘配额、网络开关、最大进程数五个维度限制 Tool。"
            "默认断网：Tool 不该偷偷访问外网，需要网络的能力必须显式开白名单。"
            "资源耗尽（Resource Exhausted）是可重试错误，但重试前应当降低资源需求或换节点。"
        ),
        tags=["沙箱", "sandbox", "资源", "隔离"],
    ),
    KnowledgeDoc(
        doc_id="DOC-006",
        title="沙箱超时必须大于 Tool 超时",
        content=(
            "如果沙箱超时 60s、Tool 超时也是 60s，两者会同时触发，"
            "于是「到底是 Tool 自己超时还是被沙箱杀掉」永远说不清，"
            "错误分类和恢复动作都会变得不可靠。"
            "约定：Sandbox Timeout > Tool Timeout（例如 65s > 60s），"
            "让 Tool 层先优雅失败，沙箱超时只作为最后一道兜底。"
        ),
        tags=["沙箱", "超时", "timeout"],
    ),
    KnowledgeDoc(
        doc_id="DOC-007",
        title="错误分类：先分类，再决定恢复动作",
        content=(
            "FAILED 不能简单理解成 FAILED -> retry。错误分为校验错误、权限错误、超时、"
            "网络错误、资源耗尽、Tool 不存在、业务错误、沙箱错误、内部错误。"
            "分类信息在抛出点就挂在异常实例上，由分类器读出后交给恢复策略，"
            "避免在错误处理里反推错误类型这种脆弱写法。"
        ),
        tags=["错误", "error", "分类", "分类器"],
    ),
    KnowledgeDoc(
        doc_id="DOC-008",
        title="恢复策略表：Retry / Repair / Fallback / Human / Abort",
        content=(
            "校验错误走 Repair（参数自愈），超时与网络错误走 Retry，"
            "Tool 不存在走 Fallback（换等价 Tool），"
            "权限错误与业务错误走 Abort（重试没有意义），"
            "高风险且状态不确定走 Human（人工确认）。"
            "这张表按错误类型给出默认动作，可以被每个 Tool 的策略覆盖。"
        ),
        tags=["恢复", "recovery", "策略", "降级", "fallback"],
    ),
    KnowledgeDoc(
        doc_id="DOC-009",
        title="指数退避与抖动",
        content=(
            "重试间隔按指数增长并叠加随机抖动，抖动比例通常取 0.3。"
            "必须加抖动的原因：大量 Agent 同时失败会同时重试，"
            "整齐的重试波峰会把下游服务再次打垮（重试风暴）。"
            "随机化把波峰摊平成斜坡，这是「优雅降级」在时间维度上的体现。"
        ),
        tags=["重试", "retry", "退避", "backoff", "抖动"],
    ),
    KnowledgeDoc(
        doc_id="DOC-010",
        title="大结果处理：inline 与 artifact 的 32KB 分界",
        content=(
            "Tool 返回 100MB 日志时不可能把 100MB 塞进 LLM Context。"
            "结果处理器以 32KB 为界：小于等于阈值直接 inline 进上下文；"
            "超过阈值则把完整结果写入对象存储，只把 artifact_id 与预览交回 Agent。"
            "阈值用固定字节数而不是按 token 估算，是为了让行为与模型解耦、可复现。"
        ),
        tags=["结果", "result", "大结果", "artifact", "截断"],
    ),
    KnowledgeDoc(
        doc_id="DOC-011",
        title="预览截断：前 8KB + 后 8KB + 统计信息",
        content=(
            "大结果的预览取前 8KB 与后 8KB 拼接，并附上总长度、总行数等统计。"
            "为什么尾部也要留：最有价值的信息往往在末尾 —— 堆栈的最后一行、"
            "测试报告结尾的失败计数、日志最后的退出码。只留头部会把结论切掉，"
            "Agent 就不得不下载整个 artifact，反而更费上下文。"
        ),
        tags=["预览", "preview", "截断", "结果"],
    ),
    KnowledgeDoc(
        doc_id="DOC-012",
        title="执行事件流：审计、排障、恢复与可观测性",
        content=(
            "每一次执行都追加结构化事件：创建、校验、入队、抢租约、进度、完成、失败。"
            "事件流同时服务于四件事：审计（谁在什么时候做了什么）、"
            "排障（失败前最后一条事件是什么）、恢复（崩溃后判断执行到哪一步）、"
            "可观测性（成功率和耗时分布）。事件必须与执行状态同事务提交。"
        ),
        tags=["审计", "事件", "event", "可观测"],
    ),
    KnowledgeDoc(
        doc_id="DOC-013",
        title="Outbox 模式：先提交数据库，再更新缓存",
        content=(
            "数据库提交成功后，把「已成功」这件事写进 outbox 表，"
            "再由投递器异步更新 Redis。顺序必须如此，反过来会出现"
            "「Redis 说 SUCCESS、数据库还说 PROCESSING」的撕裂窗口："
            "Agent 拿着 SUCCESS 去查结果却查不到，只能重试，"
            "而重试又会命中同一个 SUCCESS，于是陷入死循环。"
        ),
        tags=["outbox", "事务", "一致性", "缓存"],
    ),
    KnowledgeDoc(
        doc_id="DOC-014",
        title="循环检测：WARNING / DEGRADED / STOP 逐级升级",
        content=(
            "发现一次重复调用不该立刻停止 —— 循环调用在轮询场景里是合法意图。"
            "策略是逐级升级：同一执行签名在时间窗内重复 2 次给 WARNING，"
            "3 次降级（缩短上下文、降低 top_k、换便宜的模型），"
            "5 次才 STOP。另外还要做调用图层面的环检测，挡住 A->B->A->B 这种间接循环。"
        ),
        tags=["循环", "loop", "重复", "检测", "降级"],
    ),
    KnowledgeDoc(
        doc_id="DOC-015",
        title="参数注入防护：路径穿越、命令注入、SQL 注入",
        content=(
            "所有参数在进入执行前做确定性检查：路径必须落在白名单前缀之下且不出现 ../，"
            "命令参数只允许白名单可执行文件，SQL 片段检查危险关键字。"
            "注入命中一律不重试（属于安全事件），并留下审计事件。"
            "防护要在参数自愈之后执行，否则自愈会把被拦下的参数「修」回来。"
        ),
        tags=["安全", "注入", "injection", "路径"],
    ),
    KnowledgeDoc(
        doc_id="DOC-016",
        title="人工审批：高风险 Tool 进入 WAITING_HUMAN",
        content=(
            "风险等级为高的 Tool（生产部署、数据库删除之类）不自动执行，"
            "而是生成审批单进入 WAITING_HUMAN，等待人类批准、拒绝或修改参数。"
            "权限与审批是两件事：管理员有 database.delete 权限，"
            "仍然需要走一次人工确认 —— 权限解决「能不能」，审批解决「该不该」。"
        ),
        tags=["审批", "approval", "人工", "风险", "高风险"],
    ),
    KnowledgeDoc(
        doc_id="DOC-017",
        title="非幂等副作用的崩溃语义",
        content=(
            "幂等性等级分四档：纯函数可直接重试，幂等操作可接管重跑，"
            "至少一次语义要先查询状态再决定，非幂等副作用（支付、删除）"
            "崩溃后必须人工确认或依赖外部事务号对账。"
            "给非幂等 Tool 配置「任何错误都不重试」是默认动作，"
            "但真正危险的是崩溃 —— 那连错误都没有，只有不确定。"
        ),
        tags=["幂等", "非幂等", "崩溃", "副作用"],
    ),
    KnowledgeDoc(
        doc_id="DOC-018",
        title="结果与状态必须同事务提交",
        content=(
            "写结果和更新执行状态必须在同一个数据库事务里完成，"
            "任何「先写结果再改状态」的两步写法都会留下中间态："
            "进程死在两步之间，就会出现执行状态是成功、结果表里却没有行的情况。"
            "同事务提交是硬约束，不是性能优化 —— 它保证"
            "「查得到结果」与「状态是成功」这两件事永远同时为真。"
        ),
        tags=["事务", "结果", "一致性", "状态"],
    ),
]


class SearchKnowledgeTool(BaseTool):
    """内置知识库检索（sync / ~2s / risk=low / idempotency=pure）。"""

    name: ClassVar[str] = "search_knowledge"
    category: ClassVar[str] = "sync.read"
    args_model: ClassVar[Type[BaseModel]] = SearchArgs

    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        name="search_knowledge",
        version="1.0",
        description=(
            "在平台内置的技术知识库中检索文档片段。"
            "mode=semantic 做模糊语义召回并可用标签加权；"
            "mode=keyword 只做字面命中，更便宜，是 §33 降级后的替代路径。"
        ),
        execution_mode=ExecutionMode.SYNC,
        estimated_duration_ms=2000,
        timeout_ms=10000,
        cpu_limit=1.0,
        memory_limit_mb=512,
        network_access=False,
        idempotent=True,
        risk_level=RiskLevel.LOW,
        required_permissions=["knowledge.read"],
        idempotency_level=IdempotencyLevel.PURE,
    )

    # 降级时的 top_k 上限（§33：只能调小，见类 docstring 的说明）
    _DEGRADED_TOP_K: ClassVar[int] = 3

    # ------------------------------------------------------------------
    def run(self, args: SearchArgs, ctx: ToolContext) -> ToolOutput:
        """检索知识库。

        两种模式的算法在 docstring 里写死，是为了让「同一 query 结果不同」
        这件事**可预期**：评估降级策略时，我们需要知道差异来自哪里。

        - ``semantic``：把 query 切成字符 bigram，与文档（标题 + 正文 + 标签）
          的 bigram 求重合率，再叠加标签命中加权。特点是**字面不像但语义相近**
          也能召回（"租约过期" 命中含 "心跳超时" 的文档）。
        - ``keyword``：把 query 按空白/标点切词，要求词在文档里**精确出现**，
          按出现次数与字段权重打分。召回少而准，无命中就返回空列表。
        """
        mode, top_k, degraded = self._resolve_effective_args(args, ctx)

        docs = self._rank(args.query, mode)
        hits = [
            {
                "doc_id": doc.doc_id,
                "title": doc.title,
                "snippet": self._snippet(doc.content, args.query),
                "score": score,
            }
            for doc, score in docs[:top_k]
        ]

        # 无命中不是错误（§34 不把「空集」当失败）：
        # 检索无结果是一个**合法结论**，把它变成异常会让 Agent 无法区分
        # 「知识库里确实没有」和「检索系统坏了」。
        return ToolOutput(
            data={
                "query": args.query,
                "mode": mode,
                "top_k": top_k,
                "hits": hits,
                "total": len(hits),
            },
            stats={
                "duration_ms": ctx.elapsed_ms(),
                "degraded": degraded,
                "candidate_count": len(docs),
            },
        )

    # ------------------------------------------------------------------
    def _resolve_effective_args(
        self, args: SearchArgs, ctx: ToolContext
    ) -> tuple[str, int, bool]:
        """计算**实际生效**的 mode / top_k —— §33 降级的注入点。

        降级提示放在 ``ctx.call.arguments`` 而不是 args_model 里，是刻意的：
        降级是**平台的决策**（循环检测降级、超时降级、人类修改参数），
        不该成为 Tool 对 LLM 暴露的一个「可自由填写」的开关 ——
        否则模型学会写 ``degraded=true`` 来换便宜路径，降级就从
        「可靠性策略」退化成「模型逃课手段」。

        :returns: ``(mode, top_k, degraded)``。
        """
        raw_degraded = (ctx.call.arguments or {}).get("degraded")
        if not raw_degraded:
            return args.mode, args.top_k, False

        # 降级必须是「单向下调」：mode 从 semantic 退到 keyword（更便宜），
        # top_k 只能取更小的值。任何「降级后反而更强」的结果都说明写反了。
        mode = "keyword"
        top_k = max(1, min(args.top_k, self._DEGRADED_TOP_K))
        ctx.logger.info("search 降级生效 query=%r top_k=%d", args.query, top_k)
        return mode, top_k, True

    # ------------------------------------------------------------------
    def _rank(self, query: str, mode: str) -> list[tuple[KnowledgeDoc, float]]:
        """按模式打分排序，只保留有命中的文档（分数 > 0）。"""
        if mode == "keyword":
            scored = [(doc, self._keyword_score(query, doc)) for doc in _KNOWLEDGE_BASE]
        else:
            scored = [(doc, self._semantic_score(query, doc)) for doc in _KNOWLEDGE_BASE]
        hits = [(doc, round(score, 4)) for doc, score in scored if score > 0]
        # 分数相同的按 doc_id 排序：让检索结果**稳定**，
        # 否则同一 query 两次调用给出不同顺序，会让「幂等」在观感上破产。
        hits.sort(key=lambda pair: (-pair[1], pair[0].doc_id))
        return hits

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        """字符 bigram 集合。中文没有空格分词，bigram 是 stdlib 下最省事的相关性信号。"""
        cleaned = re.sub(r"[\s\W_]+", "", text.lower())
        return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}

    @classmethod
    def _semantic_score(cls, query: str, doc: KnowledgeDoc) -> float:
        """模糊召回：bigram 重合率（0.7 权重）+ 标签命中率（0.3 权重）。"""
        query_grams = cls._bigrams(query)
        if not query_grams:
            return 0.0
        doc_grams = cls._bigrams(f"{doc.title}{doc.content}")
        overlap = len(query_grams & doc_grams) / len(query_grams)

        tag_hit = 0.0
        if doc.tags:
            lowered = query.lower()
            tag_hit = sum(
                1 for tag in doc.tags if tag.lower() in lowered or lowered in tag.lower()
            ) / len(doc.tags)

        return overlap * 0.7 + tag_hit * 0.3

    @staticmethod
    def _keyword_score(query: str, doc: KnowledgeDoc) -> float:
        """精确命中：词必须在标题/正文里**原样出现**，按出现次数与字段权重计分。

        标题权重高于正文，标签权重最高 —— 标签是人工标注的主题，
        比正文里偶然出现的词更能说明「这篇文档讲的就是这件事」。
        """
        tokens = [t for t in re.split(r"[\s,，。;；:：!！?？/、]+", query.strip()) if t]
        if not tokens:
            return 0.0
        haystack = f"{doc.title}\n{doc.content}".lower()
        doc_tags = " ".join(doc.tags).lower()

        hits = 0
        score = 0.0
        for token in tokens:
            lowered = token.lower()
            count = haystack.count(lowered)
            if count == 0 and lowered not in doc_tags:
                continue
            hits += 1
            score += min(count, 3) * 0.1                      # 正文出现次数（封顶，避免长文霸榜）
            score += 0.5 if lowered in doc.title.lower() else 0.0
            score += 0.8 if lowered in doc_tags else 0.0
        if hits == 0:
            return 0.0
        # 必须**所有**词都命中才计满分：keyword 模式的语义就是「都说到才相关」
        return score * (hits / len(tokens))

    @staticmethod
    def _snippet(content: str, query: str, width: int = 140) -> str:
        """截一段以命中词为中心的摘要；没命中就取开头。"""
        lowered = content.lower()
        for token in re.split(r"\s+", query.strip()):
            if not token:
                continue
            pos = lowered.find(token.lower())
            if pos >= 0:
                start = max(0, pos - width // 3)
                return ("..." if start > 0 else "") + content[start : start + width] + "..."
        return content[:width] + ("..." if len(content) > width else "")
