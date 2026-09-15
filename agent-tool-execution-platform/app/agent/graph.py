"""Agent Runtime —— LangGraph 图 + Tool Node（说明书 §48 / §49 / §50 / §52）。

§48 是本文件存在的理由：Tool Node **不直接执行 Tool**
--------------------------------------------------------
说明书把错误示范和正确示范并排放在一起，差别只有几行，后果却完全不同。

错误示范（Agent = 执行者）::

    def tool_node(state):
        spec = registry.get(state["tool_name"])
        result = spec.tool.run(args, ctx)      # ← 直接调 Python 函数
        return {"tool_results": {**state["tool_results"], name: result}}

这一行 ``spec.tool.run(...)`` 一次性绕过平台的全部能力：

* **没有幂等**：重放一次就真的再执行一次（重复扣款/重复建单，§7-§12）；
* **没有校验与自愈**：参数不合法只能在 Tool 内部炸掉（§21-§25）；
* **没有权限**：Agent 想调什么就调什么（§26）；
* **没有沙箱与资源限制**：Tool 在 Agent 进程里裸跑（§27-§29）；
* **没有审计**：事后没人知道「谁在什么时候因为什么调了它」（§42）；
* **没有恢复语义**：崩溃之后连「它到底跑没跑」都无从查起（§19/§55）。

正确示范（Agent = 提交者）::

    def tool_node(state):
        call = ToolCall(...)                   # §6 标准化请求
        result = gateway.submit(call)          # ← 唯一入口
        return route_by_outcome(result)        # 按 SubmitOutcome 分支

Agent 只负责「描述想做什么」（:class:`~app.domain.models.ToolCall`），
「什么时候做、在哪做、做几次」全部交由平台裁决。
本模块的 :meth:`AgentRuntime._tool_node` 就是上面这段正确示范的完整版。

SubmitOutcome 的六个分支（§48 原代码语义）
------------------------------------------
======================== ==========================================================
outcome                   Tool Node 的动作
======================== ==========================================================
``EXECUTED``             同步执行完毕 -> 写 ``tool_results``，``status=COMPLETED``
``DEDUPLICATED``         幂等命中 SUCCESS（§12）-> 直接复用结果，**绝不重跑**
``ACCEPTED``             异步已入队 -> 写 ``pending_tool_call``，``interrupt()`` 让出
``WAITING``              幂等命中 PROCESSING 且租约有效（§11）-> 同 ACCEPTED
``WAITING_HUMAN``        高风险拦截（§51）-> ``interrupt()`` 等人工决策
``REJECTED`` / ``FAILED`` 校验/权限/注入拦截或执行失败 -> 写 error，**不重试**
======================== ==========================================================

§49：长任务为什么要 ``interrupt()`` 而不是「一直等」
-----------------------------------------------------
一个 ``run_test`` 要跑 1~5 分钟。如果 Tool Node 里写 ``time.sleep`` 或阻塞轮询，
一个 Worker 就被这一次调用钉死了 —— 并发度从「Worker 数」掉到「1」，
而且 Worker 一崩，这条进度就彻底没了。

说明书给的解法是四段式：

    ``Checkpoint`` -> ``Interrupt/WAIT`` -> ``Tool Event`` -> ``Resume Graph``

即：提交完就**把进度写进 Checkpoint 然后让出**，等工作真正完成的事件回来，
再拿着同一个 ``run_id`` 恢复图。:meth:`AgentRuntime.run` 在返回 WAITING_TOOL 时
占用的资源已经全部释放，:meth:`AgentRuntime.resume` 才是「回来接着走」的入口。

§52：人工审批也是 interrupt，不是「等一个全局变量」
----------------------------------------------------
``WAITING_HUMAN`` 的处置与异步 Tool 完全同构：``interrupt()`` 挂起、
人的决定作为 resume 值进来。区别只在恢复时要按 ``APPROVE`` / ``REJECT`` /
``MODIFY`` 三条路走，而不是「重查一次 Tool 状态」。把两者统一成
``interrupt`` 的好处是：**崩溃恢复路径只有一条**（读 Checkpoint -> 看 status
-> 决定 wait/take/submit），不必为「等 Tool」和「等人」写两套恢复逻辑。

一处必须知道的 LangGraph 行为
-----------------------------
``interrupt()`` 之后节点重跑是**从函数第一行**开始的，``interrupt()`` 的返回值
只是把 resume 值交回给「这一次重跑里的那个 interrupt 调用」。所以：

* 节点必须**幂等**：重跑时那次 ``gateway.submit(call)`` 会被再执行一遍 ——
  这正是 §20 说的「Checkpoint + Idempotency 必须一起用」：
  平台会按 ``idempotency_key`` 认出这是同一笔，返回 ``DEDUPLICATED`` 而不是再跑一次。
* 节点里**不能**写「只在第一次执行时才做」的副作用代码（比如 ``counter += 1``）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field

from ..config import AppConfig
from ..domain.enums import (
    ErrorType,
    ExecutionMode,
    ExecutionStatus,
    LoopSignal,
    RecoveryAction,
    RiskLevel,
    SubmitOutcome,
)
from ..domain.errors import ToolPlatformError
from ..domain.models import (
    PendingToolCall,
    ResultEnvelope,
    SubmitResult,
    ToolCall,
    compute_idempotency_key,
)
from ..tools.registry import ToolRegistry, ToolSpec
from .checkpoint import CheckpointConfig, CheckpointManager
from .state import AgentState, initial_state, request_of, summarize

logger = logging.getLogger(__name__)

# 内置规划器产出计划的上限（§47 ``max_steps`` 的运行时兜底）
MAX_PLAN_STEPS = 4


# ======================================================================
# Tool Node 的归一化产物
# ======================================================================
class ToolNodeResult(BaseModel):
    """``tool_node`` 对一次 ``gateway.submit()`` 的归一化解读。

    为什么要有这一层，而不是直接传 :class:`~app.domain.models.SubmitResult`：

    1. **SubmitResult 是平台的契约，本类是 Agent 的契约**。两者形状接近但不是一回事
       （比如 Agent 只关心 ``is_final`` / 分支标识，不关心 ``job_id`` 长什么样）。
       中间隔一层，将来平台改字段时 Agent 不会跟着塌。
    2. **它要进 Checkpoint**。落盘的必须是 JSON-safe 的纯数据，Pydantic 对象虽然
       也能序列化，但把领域模型的 Schema 写进 Agent 的历史状态会让两边版本强耦合。
    """

    model_config = ConfigDict(extra="allow")

    call_id: str
    tool_name: str
    outcome: str
    """取值见 :class:`~app.domain.enums.SubmitOutcome`。"""

    status: ExecutionStatus
    execution_mode: ExecutionMode = ExecutionMode.SYNC

    result: Optional[ResultEnvelope] = None
    result_id: Optional[str] = None

    error_type: Optional[str] = None
    error_message: Optional[str] = None

    attempt: int = 0
    deduplicated: bool = False
    duration_ms: int = 0

    resume_hint: Optional[RecoveryAction] = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_final(self) -> bool:
        """是否已经拿到终态结论（成功拿到结果，或确定失败）。"""
        return self.status in (ExecutionStatus.SUCCESS, ExecutionStatus.COMPLETED)

    @classmethod
    def from_submit(cls, submit: SubmitResult, *, tool_name: str) -> "ToolNodeResult":
        """把平台返回值压成 Agent 契约。"""
        return cls(
            call_id=submit.call_id,
            tool_name=tool_name,
            outcome=str(submit.outcome),
            status=submit.status,
            execution_mode=submit.execution_mode,
            result=submit.result,
            result_id=submit.result_id,
            error_type=submit.error_type,
            error_message=submit.error_message,
            attempt=submit.attempt,
            deduplicated=submit.deduplicated,
            duration_ms=submit.duration_ms,
            resume_hint=submit.resume_hint,
            detail=dict(submit.detail or {}),
        )

    def to_ledger(self) -> dict[str, Any]:
        """写进 ``state["tool_results"][call_id]`` 与 ``plan[i]`` 的紧凑账目。"""
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "outcome": self.outcome,
            "status": self.status.value,
            "deduplicated": self.deduplicated,
            "duration_ms": self.duration_ms,
            "result_id": self.result_id,
            "result": self.result.to_agent_view() if self.result is not None else None,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


# ======================================================================
# 内置确定性规划器（离线可跑，不调 LLM）
# ======================================================================
#: 关键词 -> Tool 的映射表。顺序即优先级（先匹配到的胜出）。
#: **刻意不用 LLM**：Demo 必须离线可复现 —— 同一句请求每次都规划出同一个计划，
#: 崩溃恢复才能被对比验证（§19）；靠 LLM 规划的话「恢复」和「重跑」根本分不清。
#: **不要往里加「加 / 减 / 除」这类单字**：中文里它们藏在别的词里 ——
#: 「删**除**」会让一句删除请求被同时规划出 ``calculator`` 与 ``database_delete``，
#: 于是 Demo 里出现「用户想删数据，Agent 先算了个 1+1」这种荒唐计划。
#: 关键词要长到足以表达意图，短词只在明确无歧义时才用（如 ``算`` / ``tc`` / ``py``）。
_KEYWORD_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("计算", "算", "乘", "表达式", "expression", "calculator"), "calculator"),
    (("测试", "用例", "tc", "pytest", "跑测"), "run_test"),
    (("查", "搜索", "检索", "知识", "search"), "search_knowledge"),
    (("python", "py", "代码", "脚本"), "execute_python"),
    (("分析", "大文件", "日志", "log", "file"), "large_file_analysis"),
    (("删除", "delete", "清理", "drop"), "database_delete"),
)

#: 从请求里抽取参数的确定性正则。
_RE_EXPRESSION = re.compile(r"\d+(?:\s*[-+*/]\s*\d+)+")
_RE_TEST_CASE = re.compile(r"TC\d{3}", re.IGNORECASE)
_RE_TABLE = re.compile(r"(demo_[a-z_]+)")
_RE_PATH = re.compile(r"(/?workspaces?/[\w./\-]+)")
_RE_CODE = re.compile(r"(?:python|代码|脚本)[:：]?\s*(.+)", re.IGNORECASE)


def _extract_arguments(tool_name: str, request: str, spec: Optional[ToolSpec]) -> dict:
    """按 Tool 名从请求里确定性抽取参数。

    两级策略：

    1. **按 Tool 名的已知字段填值**（``configs/tools.yaml`` 里声明的那些名字）。
    2. **用 ``args_model`` 的字段名过滤**：模型里没有的键一律丢掉。
       这一步不是洁癖 —— Gateway 会用 ``args_model`` 做校验，
       多传一个未知键会直接变成 ``REJECTED``，把一次本可成功的调用打成失败。

    ``spec`` 为 ``None``（Tool 还没注册）时退化成第 1 步的原样输出，
    让「工具缺失」在 Gateway 侧以 ``TOOL_NOT_FOUND`` 的形式暴露出来，
    而不是在规划阶段就被悄悄吞掉。
    """
    request = request or ""
    arguments: dict[str, Any] = {}

    expr = _RE_EXPRESSION.search(request)
    test_case = _RE_TEST_CASE.search(request)
    table = _RE_TABLE.search(request)
    path = _RE_PATH.search(request)
    code = _RE_CODE.search(request)

    if tool_name == "calculator":
        arguments["expression"] = expr.group(0).replace(" ", "") if expr else "1+1"

    elif tool_name == "search_knowledge":
        arguments["query"] = request[:400] or "默认查询"
        arguments["top_k"] = 3

    elif tool_name == "run_test":
        arguments["test_cases"] = [test_case.group(0).upper()] if test_case else ["TC001"]
        arguments["timeout"] = 60
        arguments["path"] = path.group(1) if path else "/workspace/tests/"

    elif tool_name == "execute_python":
        arguments["code"] = code.group(1).strip() if code else "print('hello')"

    elif tool_name == "large_file_analysis":
        arguments["path"] = path.group(1) if path else "/workspace/data/app.log"

    elif tool_name == "database_delete":
        arguments["table"] = table.group(1) if table else "demo_orders"
        arguments["where"] = "id = 0"

    # --- 第二级：用参数模型的字段名做收口 ---
    if spec is not None and hasattr(spec.args_model, "model_fields"):
        allowed = set(spec.args_model.model_fields)
        arguments = {k: v for k, v in arguments.items() if k in allowed}

    return arguments


def default_planner(request: str, registry: ToolRegistry) -> list[dict]:
    """内置确定性规划器：关键词选 Tool + 正则抽参数。

    产出形如 ``[{"step_id": "step_1", "tool_name": "calculator", "arguments": {...}}]``，
    至少 1 步、最多 :data:`MAX_PLAN_STEPS` 步。

    **步骤顺序 = :data:`_KEYWORD_RULES` 的表顺序，不是请求里的出现顺序。**
    这是刻意的：不做句序解析，换来「同一个请求永远规划出同一个计划」——
    而计划进 Checkpoint 之后必须可复现，否则「恢复」和「重来」就分不清了（§19）。

    为什么「至少 1 步」而不是「匹配不到就返回空计划」：空计划会让图直接走到 END，
    演示时看到的是一个「什么都没做就成功」的结果，比失败还难排查。
    匹配不到关键词时退化为 ``calculator`` —— 一个纯函数、无副作用、
    一定能在离线环境跑完的 Tool，让流程保持可观测。

    ``registry`` 有两个用途：一是过滤掉**没注册**的 Tool（否则会白白提交一次
    ``TOOL_NOT_FOUND``），二是把 ``args_model`` 的字段名交给
    :func:`_extract_arguments` 做参数收口。
    """
    text = (request or "").lower()
    chosen: list[tuple[str, Optional[ToolSpec]]] = []

    for keywords, tool_name in _KEYWORD_RULES:
        if not any(keyword.lower() in text for keyword in keywords):
            continue
        if not registry.has(tool_name):
            # Tool 没注册：跳过而不是硬提交。硬提交的后果是每一步都 FAILED，
            # 而那会把「环境没装好」误报成「业务失败」。
            logger.debug("规划命中 %s 但未注册，跳过", tool_name)
            continue
        chosen.append((tool_name, registry.get(tool_name)))

    if not chosen:
        fallback = "calculator" if registry.has("calculator") else None
        if fallback is None:
            names = registry.names()
            if not names:
                return []
            fallback = names[0]
        chosen.append((fallback, registry.get(fallback)))

    plan: list[dict] = []
    for index, (tool_name, spec) in enumerate(chosen[:MAX_PLAN_STEPS], start=1):
        plan.append(
            {
                # logical_step_id 是幂等键的第三分量（§7）：同一步骤重复提交要拦，
                # 不同步骤调同一个 Tool 要放 —— 所以 step_id 必须稳定且唯一。
                "step_id": f"step_{index}",
                "tool_name": tool_name,
                "arguments": _extract_arguments(tool_name, request, spec),
                "call_id": None,
                "status": "PENDING",
            }
        )
    return plan


# ======================================================================
# AgentRuntime
# ======================================================================
class AgentRuntime:
    """把「规划 -> 提交 Tool -> 分支处置」编译成一张 LangGraph 图并驱动它。

    :param gateway: :class:`ToolGateway`，**Agent 与平台之间的唯一通道**。
        本类不 import 它的具体实现，只用鸭子类型调 6 个方法（见模块 docstring
        与 README 的接口清单），因此换实现（内存版 / HTTP 版）不需要改这里一行。
    :param registry: :class:`~app.tools.registry.ToolRegistry`，规划器据此选 Tool、
        取 ``args_model`` 收口参数。
    :param config: 平台配置。缺省从 ``registry.config`` 继承，再缺省
        :meth:`AppConfig.for_demo`。
    :param checkpoint: :class:`CheckpointManager`。不传则按 config 建一个落盘的
        （``checkpoints.sqlite``）——**刻意不用 ``:memory:``**：
        内存库在进程退出时就没了，而「崩溃恢复」正是本模块要演示的东西。
    :param planner: ``(request, registry) -> list[dict]`` 的计划函数。
        缺省 :func:`default_planner`（确定性、离线可跑）。
        接 LLM 规划器时注意：它必须**纯函数**，因为节点重跑时会再调一次。
    :param approvals: §52 人工审批台账（可选）。只在
        ``SubmitResult.detail["approval_id"]`` 存在时被透传进
        ``interrupt()`` 的载荷里，Agent 自己**不**做审批决策 ——
        「谁有资格批」是平台（:mod:`app.human`）的事，不是 Agent 的事。
    """

    def __init__(
        self,
        gateway: Any,
        registry: ToolRegistry,
        *,
        config: Optional[AppConfig] = None,
        checkpoint: Optional[CheckpointManager] = None,
        planner: Optional[Callable[[str, ToolRegistry], list[dict]]] = None,
        approvals: Any = None,
    ) -> None:
        self.gateway = gateway
        self.registry = registry
        self.config = config or getattr(registry, "config", None) or AppConfig.for_demo()
        self.planner = planner or default_planner
        self.approvals = approvals

        # checkpoint 的库路径与平台 DB 分开：一个是 LangGraph 的图状态，
        # 一个是平台事实表（§42/§44），混在一张库里会让「谁写坏了谁」无法定位。
        self.checkpoint = checkpoint or CheckpointManager(
            CheckpointConfig(db_path=self._default_checkpoint_path())
        )
        self._owns_checkpoint = checkpoint is None

        self._graph: Any = None

    # ==================================================================
    # 装配
    # ==================================================================
    def _default_checkpoint_path(self) -> str:
        """从平台配置推导 checkpoint 库路径。

        ``db_path`` 为 ``:memory:`` 时无法共用（一份内存库不能被两个连接看到），
        退回同目录下的 ``checkpoints.sqlite``；否则与平台库同目录，
        便于演示脚本一次性清理。
        """
        db_path = getattr(self.config, "db_path", ":memory:") or ":memory:"
        if db_path == ":memory:":
            return "checkpoints.sqlite"
        import os

        directory = os.path.dirname(os.path.abspath(db_path))
        return os.path.join(directory, "checkpoints.sqlite")

    @property
    def graph(self) -> Any:
        """编译好的图（懒加载 + 缓存）。"""
        if self._graph is None:
            self._graph = self.build()
        return self._graph

    def build(self) -> Any:
        """编译图并挂上 checkpointer。

        结构::

            START -> planner -> tool -> (条件边) -> tool | END

        为什么 ``tool -> tool`` 是一条**自环**而不是「每一步一个节点」：
        ``plan`` 是数据，不是拓扑。把 4 个步骤展开成 4 个节点，等于把计划
        焊死在编译期 —— 恢复时计划一变（人工 MODIFY、循环降级换 fallback）
        就得重新编译图，而那会**换掉 checkpointer 上的图结构**，
        让「恢复到原来的 thread」变成一件危险的事。自环 + ``step_index``
        让拓扑保持常量，计划完全由状态驱动。
        """
        builder = StateGraph(AgentState)
        builder.add_node("planner", self._planner_node)
        builder.add_node("tool", self._tool_node)

        builder.add_edge(START, "planner")
        builder.add_edge("planner", "tool")
        builder.add_conditional_edges(
            "tool",
            self._route_after_tool,
            {"tool": "tool", "end": END},
        )
        return builder.compile(checkpointer=self.checkpoint.saver())

    # ==================================================================
    # 节点 1：规划
    # ==================================================================
    def _planner_node(self, state: AgentState) -> dict:
        """把用户请求变成一份**确定的**步骤计划，并写进 Checkpoint。

        计划进状态而不是每次现算，是为了让「恢复」真的等于「续跑」：
        如果恢复时重新规划，一旦规划结果与上次不同（换了 Tool、换了参数），
        整个恢复流程的语义就崩了 —— 那叫重跑，不叫恢复（§18/§19）。

        注意本节点是**纯函数式**的（读 state、返回增量 dict），
        因为任何节点都可能在 ``interrupt()`` 之后被重跑。
        """
        request = request_of(state)
        plan = self.planner(request, self.registry)
        tool_names = [step["tool_name"] for step in plan]

        logger.info(
            "run=%s 规划完成: %s", state.get("run_id"), " -> ".join(tool_names) or "(空)"
        )

        return {
            "plan": plan,
            "step_index": 0,
            "current_step": "planner",
            "status": "PLANNING",
            "checkpoint_reason": "plan_ready",
            "messages": [
                {
                    "role": "assistant",
                    "content": f"规划完成，共 {len(plan)} 步: {tool_names}",
                    "node": "planner",
                }
            ],
        }

    # ==================================================================
    # 节点 2：Tool Node —— 只提交，不执行（§48）
    # ==================================================================
    def _tool_node(self, state: AgentState) -> dict:
        """**提交**下一步 Tool 调用并按 ``SubmitOutcome`` 分支（§48 / §49 / §52）。

        这里的每一行都对应说明书的一节：

        * 构造 :class:`~app.domain.models.ToolCall` 而不调 ``spec.tool.run``   -> §48
        * ``idempotency_key`` 由 §7 配方算出，带 ``run_id`` 与 ``step_id``     -> §7
        * ``ACCEPTED`` / ``WAITING`` -> ``interrupt()`` 让出 Worker            -> §49
        * ``WAITING_HUMAN`` -> ``interrupt()`` 等人工，恢复后读
          ``state["human_decision"]`` 走 APPROVE / REJECT / MODIFY            -> §52
        * ``REJECTED`` / ``FAILED`` -> 写 ``error`` 并且**不重试**             -> §34/§35
        * ``DEDUPLICATED`` -> 直接复用结果，这是「重跑节点但 Tool 不重跑」
          能成立的唯一原因                                                    -> §12/§20

        **重入安全**：``interrupt()`` 之后本函数会从第一行重跑，
        所以那次 ``gateway.submit()`` 会被再调一次。这不是 bug，是设计：
        第二次提交带着同一个 ``idempotency_key``，平台的幂等层会把它判成
        ``DEDUPLICATED`` 并直接给结果，于是节点自然收敛。

        **唯一必须在顶部短路的情况**：人工审批（§52）。带着 ``MODIFY`` 醒来时
        绝不能拿**原始参数**再提交一次 —— 那个提交恰恰就是被人改掉的那一笔。
        """
        plan = list(state.get("plan") or [])
        index = state.get("step_index", 0)

        if index >= len(plan):
            # 计划已经走完（正常情况下条件边会先一步路由到 END，这里是兜底）
            return {"status": "COMPLETED", "checkpoint_reason": "plan_exhausted"}

        step = dict(plan[index])
        tool_name = str(step.get("tool_name") or "")
        arguments = dict(step.get("arguments") or {})

        call = self._build_call(state, step, arguments)

        # ---- 短路：人工已经做了决定（APPROVE / REJECT / MODIFY）----
        # 判据是「status 停在 WAITING_HUMAN」+「human_decision 非空」：
        # 前者由 AgentRuntime._record_wait 写进 Checkpoint，后者由 resume() 写入。
        # 两个都在，才说明「这一次重跑是为了落实人的决定」，而不是首次执行。
        if state.get("status") == "WAITING_HUMAN" and state.get("human_decision"):
            return self._apply_human_decision(state, step, call, None, None)

        logger.info(
            "run=%s step=%s submit %s call=%s",
            state.get("run_id"),
            step.get("step_id"),
            tool_name,
            call.call_id,
        )

        try:
            submit = self.gateway.submit(call)
        except ToolPlatformError as exc:
            # 平台在提交阶段就拒绝（校验/权限/注入），不会返回 SubmitResult。
            # 这类错误**不该重试**：参数再提交一次还是同样的参数（§35 权限 -> ABORT）。
            return self._failed(
                state, step, call, exc.error_type.value, exc.message, outcome="REJECTED"
            )
        except Exception as exc:  # pragma: no cover - 网关自身故障
            return self._failed(
                state, step, call, ErrorType.INTERNAL_ERROR.value, str(exc), outcome="FAILED"
            )

        node_result = ToolNodeResult.from_submit(submit, tool_name=tool_name)
        outcome = node_result.outcome

        # ---------------------------------------------------------- 成功路径
        if outcome in (SubmitOutcome.EXECUTED.value, SubmitOutcome.DEDUPLICATED.value):
            return self._completed(state, step, node_result)

        # ------------------------------------------- 异步已受理：让出，等 Tool Event
        if outcome == SubmitOutcome.ACCEPTED.value:
            # §49：**不在这里 sleep 等结果**。写清 pending 就 interrupt，
            # 等 Tool 完成事件（或轮询发现终态）再 resume。
            # 注意：下面的 interrupt() 会抛出 GraphInterrupt，
            # 本函数返回的 dict 不会被写入 —— pending_tool_call 由
            # AgentRuntime.run() 在捕获到 __interrupt__ 之后补写进 Checkpoint。
            interrupt(
                {
                    "type": "WAITING_TOOL",
                    "run_id": state.get("run_id"),
                    "call_id": node_result.call_id,
                    "tool_name": tool_name,
                    "step_id": step.get("step_id"),
                    "idempotency_key": call.idempotency_key,
                    "submitted_at": call.created_at.isoformat(),
                    "attempt": call.attempt,
                    # §18 规范的「待完成调用」原样带出去：恢复方拿到这个 dict
                    # 就能直接喂给 AgentRecoveryManager.plan_resume()。
                    "pending": self._pending_of(call, step).model_dump(mode="json"),
                    "reason": "异步 Tool 已入队，等 Tool Event 后 resume 本图（§49）",
                }
            )
            # 走到这里说明已经被 resume 了：重跑时 submit 会命中幂等，
            # 返回 DEDUPLICATED（已完成）或 WAITING（还在跑）。
            return self._recheck_after_resume(state, step, call, node_result)

        # -------------------------------------- 幂等命中 PROCESSING：同样是让出（§11）
        if outcome == SubmitOutcome.WAITING.value:
            interrupt(
                {
                    "type": "WAITING_TOOL",
                    "run_id": state.get("run_id"),
                    "call_id": node_result.call_id,
                    "tool_name": tool_name,
                    "step_id": step.get("step_id"),
                    "idempotency_key": call.idempotency_key,
                    "owner_action": str(submit.resume_hint) if submit.resume_hint else None,
                    "pending": self._pending_of(call, step).model_dump(mode="json"),
                    "reason": (
                        "幂等键命中 PROCESSING 且租约有效：另一个 Worker 正在跑，"
                        "重复提交没有意义，只能等（§11）"
                    ),
                    "detail": submit.detail,
                }
            )
            return self._recheck_after_resume(state, step, call, node_result)

        # -------------------------------------------------- 高风险：人工审批（§52）
        if outcome == SubmitOutcome.WAITING_HUMAN.value:
            return self._apply_human_decision(state, step, call, submit, node_result)

        # -------------------------------------- REJECTED / FAILED：终态，不重试
        return self._failed(
            state,
            step,
            call,
            node_result.error_type or ErrorType.INTERNAL_ERROR.value,
            node_result.error_message or f"Tool 返回 {outcome}",
            outcome=outcome,
            node_result=node_result,
        )

    # ------------------------------------------------------------------
    # 人工审批分支（§52）
    # ------------------------------------------------------------------
    def _apply_human_decision(
        self,
        state: AgentState,
        step: dict,
        call: ToolCall,
        submit: Optional[SubmitResult],
        node_result: Optional[ToolNodeResult],
    ) -> dict:
        """高风险拦截后的三条路：APPROVE / REJECT / MODIFY（§52）。

        两个入口共用本方法，区别只在「人的决定到了没有」：

        * ``submit`` 非空 —— 刚被平台拦下（``WAITING_HUMAN``），决定还没来，
          于是 ``interrupt()`` 挂起等人。
        * ``submit`` 为空 —— 从 Checkpoint 恢复回来（``resume()`` 已经把
          ``human_decision`` 写进 State），直接落实决定，**不再提交原始参数**。

        恢复时读 ``state["human_decision"]``：它由 :meth:`resume` 通过
        ``graph.update_state()`` 写进来，所以「人的决定」和「图的进度」落在
        同一个 Checkpoint 里，不会出现「决定丢了但图还在等」的悬挂状态。

        三条路各自的语义（照着说明书写）：

        * ``APPROVE``：批准这次操作 → **用同一个 idempotency_key 重提交**。
          关键点：审批通过不等于换一个操作，幂等键必须不变，
          否则一次批准会被记成两笔不同的调用。
        * ``MODIFY``：人工改了参数 → 这**确实**是另一笔操作，幂等键随之改变
          （§7 的 key 含参数），所以重算 key 再提交。
        * ``REJECT``：人工否决 → 直接终态失败，**不重试**（§52 REJECT -> CANCELLED）。
        """
        tool_name = call.tool_name
        decision = state.get("human_decision")

        if not decision:
            # 还没人做决定 —— 挂起。载荷里带上「人在批什么」的全部信息，
            # 审批界面才不需要回头再查一次库。
            detail = dict(getattr(submit, "detail", None) or {})
            approval_payload = {
                "type": "WAITING_HUMAN",
                "run_id": state.get("run_id"),
                "call_id": call.call_id,
                "tool_name": tool_name,
                "step_id": step.get("step_id"),
                "idempotency_key": call.idempotency_key,
                "arguments": call.arguments,
                "risk_level": detail.get("risk_level", RiskLevel.HIGH.value),
                "reason": getattr(submit, "error_message", None)
                or "高风险 Tool 需人工审批后执行（§51/§52）",
                "options": ["APPROVE", "REJECT", "MODIFY"],
                "approval_id": detail.get("approval_id"),
                "pending": self._pending_of(call, step).model_dump(mode="json"),
            }
            returned = interrupt(approval_payload)
            # interrupt() 的返回值是 resume 时传进来的载荷；状态里的字段优先，
            # 两者取其一即可（状态字段的好处是「未 resume 也能被 recover_run 读到」）。
            decision = _normalize_decision(returned) or state.get("human_decision")

        decision = (decision or "").upper()

        if decision == "REJECT":
            logger.info("run=%s 人工否决 %s", state.get("run_id"), tool_name)
            return self._failed(
                state,
                step,
                call,
                ErrorType.PERMISSION_ERROR.value,
                "人工审批未通过（REJECT），按 §52 转 CANCELLED，不重试",
                outcome="REJECTED",
                node_result=node_result,
            )

        # APPROVE / MODIFY：带着人工决策重新提交。
        # 用 retry_count 做闸门：如果批过之后平台**再次**返回 WAITING_HUMAN，
        # 说明拦截原因不是「没人批」而是别的（例如审批单已过期），
        # 此时再 interrupt 一次只会得到同一个结果 —— 是个死循环，必须收手（§75）。
        attempts = int(state.get("retry_count", 0))
        if attempts >= 1:
            return self._failed(
                state,
                step,
                call,
                ErrorType.PERMISSION_ERROR.value,
                "人工审批通过后仍被平台拦截，停止自动推进转由人工排查",
                outcome="REJECTED",
                node_result=node_result,
            )

        plan_extra: Optional[dict] = None
        if decision == "MODIFY":
            # 参数变了 -> 逻辑上已经是另一笔操作，幂等键必须重算（§7）。
            arguments = dict(state.get("human_arguments") or call.arguments)
            step = {**step, "arguments": arguments}
            call = self._build_call(state, step, arguments)
            plan_extra = {"modified_arguments": arguments, "modified_by": "human"}
            logger.info("run=%s 人工改写参数后重提交: %s", state.get("run_id"), arguments)

        retry_submit = self.gateway.submit(call)
        node_result = ToolNodeResult.from_submit(retry_submit, tool_name=call.tool_name)

        if node_result.outcome in (
            SubmitOutcome.EXECUTED.value,
            SubmitOutcome.DEDUPLICATED.value,
        ):
            result = self._completed(state, step, node_result, plan_extra=plan_extra)
            result["retry_count"] = attempts + 1
            result["human_decision"] = decision
            return result

        if node_result.outcome in (
            SubmitOutcome.ACCEPTED.value,
            SubmitOutcome.WAITING.value,
        ):
            # 批准之后变成「正常的长任务」，按 §49 走等待路径。
            interrupt(
                {
                    "type": "WAITING_TOOL",
                    "run_id": state.get("run_id"),
                    "call_id": node_result.call_id,
                    "tool_name": call.tool_name,
                    "step_id": step.get("step_id"),
                    "idempotency_key": call.idempotency_key,
                    "human_decision": decision,
                    "pending": self._pending_of(call, step).model_dump(mode="json"),
                    "reason": "人工已批准，异步 Tool 已入队，等 Tool Event（§49/§52）",
                }
            )
            return self._recheck_after_resume(state, step, call, node_result)

        return self._failed(
            state,
            step,
            call,
            node_result.error_type or ErrorType.INTERNAL_ERROR.value,
            node_result.error_message or "人工批准后执行失败",
            outcome=node_result.outcome,
            node_result=node_result,
        )

    # ------------------------------------------------------------------
    # 节点内部的小工具
    # ------------------------------------------------------------------
    def _recheck_after_resume(
        self,
        state: AgentState,
        step: dict,
        call: ToolCall,
        node_result: ToolNodeResult,
    ) -> dict:
        """``interrupt()`` 被 resume 之后的收尾：再查一次这次调用到底成没成。

        为什么不能直接信任「resume 了就说明成了」：resume 只是「有人把我叫醒了」，
        叫醒的原因可能是 Tool 完成了，也可能是 ``AgentRecoveryManager`` 判出
        「租约还有效，接着等」。所以必须回查一次状态，才敢决定是收结果还是继续挂起。

        回查用 ``query(call_id)``（按 call_id 查），不是 ``lookup``（按幂等键查）：
        这里问的是「**我这一笔**怎么样了」，而不是「这种调用做过没有」。
        """
        try:
            latest = self.gateway.query(node_result.call_id)
        except Exception as exc:  # pragma: no cover - 网关故障
            return self._failed(
                state,
                step,
                call,
                ErrorType.INTERNAL_ERROR.value,
                f"回查 Tool 状态失败: {exc}",
                outcome="FAILED",
                node_result=node_result,
            )

        refreshed = ToolNodeResult.from_submit(latest, tool_name=call.tool_name)

        if refreshed.outcome in (
            SubmitOutcome.EXECUTED.value,
            SubmitOutcome.DEDUPLICATED.value,
        ):
            return self._completed(state, step, refreshed)

        if refreshed.status.is_pending or refreshed.outcome in (
            SubmitOutcome.ACCEPTED.value,
            SubmitOutcome.WAITING.value,
        ):
            # 还没到终态：再挂一次。图停在同一个 Checkpoint 上，
            # 不会消耗步数（step_index 没推进），资源也依然释放着。
            interrupt(
                {
                    "type": "WAITING_TOOL",
                    "run_id": state.get("run_id"),
                    "call_id": refreshed.call_id,
                    "tool_name": call.tool_name,
                    "step_id": step.get("step_id"),
                    "idempotency_key": call.idempotency_key,
                    "reason": "resume 时 Tool 仍未出终态，继续等待（§49）",
                }
            )
            return {
                "status": "WAITING_TOOL",
                "current_step": f"tool:{call.tool_name}",
                "checkpoint_reason": "tool_still_pending",
            }

        return self._failed(
            state,
            step,
            call,
            refreshed.error_type or ErrorType.INTERNAL_ERROR.value,
            refreshed.error_message or f"Tool 终态为 {refreshed.status.value}",
            outcome=refreshed.outcome,
            node_result=refreshed,
        )

    def _build_call(self, state: AgentState, step: dict, arguments: dict) -> ToolCall:
        """按 §6/§7 构造一次 Tool 调用。

        三件事必须对齐，否则幂等会失效：

        1. ``graph_run_id`` = ``run_id``：它是幂等键的第二分量（§7）。
           如果这里填了别的值，崩溃恢复后的重提交就会算出**不同的幂等键**，
           于是 Tool 被真真实实地跑第二遍 —— 这正是 §20 要防的事故。
        2. ``logical_step_id`` = ``plan`` 里的 ``step_id``：第三分量（§7）。
           同一个 run 里循环调同一个 Tool 是合法意图，靠 step_id 区分
           「同一步重复提交」（要拦）与「不同步骤各调一次」（要放）。
        3. ``idempotency_key`` 显式算好写进 call，而不是留给 Gateway 补：
           Agent 侧必须能**自己**算出这个键，恢复流程才有东西可以去 ``lookup``。
        """
        tool_name = str(step.get("tool_name") or "")
        logical_step_id = str(step.get("step_id") or "step_1")
        run_id = str(state.get("run_id") or "run_001")
        tenant_id = str(state.get("tenant_id") or "tenantA")

        key = compute_idempotency_key(
            tenant_id=tenant_id,
            workflow_run_id=run_id,
            logical_step_id=logical_step_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        return ToolCall(
            agent_id=str(state.get("agent_id") or "agent_01"),
            session_id=str(state.get("run_id") or "session_01"),
            graph_run_id=run_id,
            tenant_id=tenant_id,
            tool_name=tool_name,
            arguments=arguments,
            idempotency_key=key,
            logical_step_id=logical_step_id,
            attempt=int(state.get("retry_count", 0)),
        )

    @staticmethod
    def _pending_of(call: ToolCall, step: dict) -> PendingToolCall:
        """构造要写进 Checkpoint 的「待完成调用」（§18）。

        只留恢复所需的最小信息集：**没有它就无法回查结果**（缺 call_id）、
        **没有它就无法重提交**（缺 arguments）、
        **没有它就无法判幂等**（缺 idempotency_key）。
        """
        return PendingToolCall(
            call_id=call.call_id,
            idempotency_key=call.idempotency_key,
            tool_name=call.tool_name,
            arguments=call.arguments,
            submitted_at=call.created_at,
            attempt=call.attempt,
        )

    def _completed(
        self,
        state: AgentState,
        step: dict,
        node_result: ToolNodeResult,
        *,
        plan_extra: Optional[dict] = None,
    ) -> dict:
        """成功路径：写账、推进 ``step_index``、累加 ``execution_path``。

        ``execution_path`` 用 ``operator.add`` reducer，所以这里**只返回本步新增的那一项**
        （``[tool_name]``），绝不能返回拼接好的完整列表 —— 那会把历史重复一遍。

        :param plan_extra: 追加到步骤台账上的额外字段（例如人工 MODIFY 后的
            ``modified_arguments``）。**刻意不覆盖原有的 ``arguments``**：
            原始参数是审计事实，「人改成了什么」另记一笔，两者都要在。
        """
        call_id = node_result.call_id
        ledger = node_result.to_ledger()

        plan = list(state.get("plan") or [])
        index = int(state.get("step_index", 0))
        if 0 <= index < len(plan):
            plan[index] = {
                **plan[index],
                "call_id": call_id,
                "status": "COMPLETED",
                "outcome": node_result.outcome,
                "deduplicated": node_result.deduplicated,
                **(plan_extra or {}),
            }

        tool_results = dict(state.get("tool_results") or {})
        tool_results[call_id] = ledger

        loop_signal = self._check_loop(state, node_result.tool_name)

        logger.info(
            "run=%s step=%s %s 完成 (%s, %dms)",
            state.get("run_id"),
            step.get("step_id"),
            node_result.tool_name,
            node_result.outcome,
            node_result.duration_ms,
        )

        return {
            "plan": plan,
            "step_index": index + 1,
            "tool_results": tool_results,
            "execution_path": [node_result.tool_name],
            "current_step": f"tool:{node_result.tool_name}",
            "pending_tool_call": None,
            "status": "COMPLETED",
            "loop_signal": loop_signal.value,
            "checkpoint_reason": "tool_completed",
            "messages": [
                {
                    "role": "tool",
                    "content": (
                        f"{node_result.tool_name} -> {node_result.outcome}"
                        f"（{'幂等复用结果' if node_result.deduplicated else '实际执行'}）"
                    ),
                    "call_id": call_id,
                    "tool_name": node_result.tool_name,
                    "step_id": step.get("step_id"),
                    "result": ledger["result"],
                }
            ],
        }

    def _failed(
        self,
        state: AgentState,
        step: dict,
        call: ToolCall,
        error_type: str,
        message: str,
        *,
        outcome: str,
        node_result: Optional[ToolNodeResult] = None,
    ) -> dict:
        """终态失败：``REJECTED`` / ``FAILED`` 都走这里，**不重试**。

        为什么 Tool Node 层不做重试：重试是**平台**的职责（§35 Recovery Policy +
        §36 Backoff），它需要知道错误分类、Tool 的幂等性等级、风险等级、
        已用次数，这些 Agent 手里并不齐全。Agent 自己重试还会绕开平台的
        计数与审计，把「一次永久性故障」放大成「一场永不停止的重放」（§75）。
        """
        error = {
            "error_type": error_type,
            "message": message,
            "tool_name": call.tool_name,
            "step_id": step.get("step_id"),
            "call_id": call.call_id,
            "idempotency_key": call.idempotency_key,
            "outcome": outcome,
        }

        plan = list(state.get("plan") or [])
        index = int(state.get("step_index", 0))
        if 0 <= index < len(plan):
            plan[index] = {**plan[index], "call_id": call.call_id, "status": "FAILED"}

        logger.warning(
            "run=%s step=%s %s 失败: [%s] %s",
            state.get("run_id"),
            step.get("step_id"),
            call.tool_name,
            error_type,
            message,
        )

        return {
            "plan": plan,
            "error": error,
            "pending_tool_call": None,
            "current_step": f"tool:{call.tool_name}",
            "status": "FAILED",
            "checkpoint_reason": f"tool_{outcome.lower()}",
            "messages": [
                {
                    "role": "tool",
                    "content": f"{call.tool_name} 失败: [{error_type}] {message}",
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "error": error,
                }
            ],
        }

    def _check_loop(self, state: AgentState, tool_name: str) -> LoopSignal:
        """§31/§32 的循环检测：按「同名 Tool 已走过的次数」逐级升级。

        为什么看 ``execution_path`` 而不是自己维护计数器：``execution_path``
        是 §47 定义、§31 指定消费的那个字段，且**只增不减**（``operator.add``），
        天然就是「历史」。自己再存一份计数器等于同一事实两处维护，
        一旦恢复后两者不同步，就会出现「明明循环了却判不出」。

        §32 的三档语义：``WARNING`` 只记不拦；``DEGRADED`` 提示可降级
        （换 fallback / 降 top_k）；``STOP`` 才真正停止推进。

        **什么时候真的会响**：内置规划器产出的计划里每个 Tool 至多出现一次
        （规则表本身去重），所以正常情况下 ``seen`` 恒为 1、信号恒为 ``OK``。
        它真正要防的是两件事：

        1. 外接的 LLM 规划器（:paramref:`AgentRuntime.planner`）排出了
           ``A -> B -> A -> B`` 这种计划 —— 那正是 §31 说的 DAG 循环；
        2. 恢复/replay 让同一段路径被反复走。

        换句话说：这一层是给**可替换的规划器**留的安全阀，不是给内置规划器
        自己用的。没有它，接上 LLM 规划器的那一天就会出现「Agent 自己
        绕圈绕到超时」。
        """
        policy = self.config.loop
        seen = sum(1 for name in (state.get("execution_path") or []) if name == tool_name) + 1

        if seen >= policy.cycle_stop:
            return LoopSignal.STOP
        if seen >= policy.cycle_degrade:
            return LoopSignal.DEGRADED
        if seen >= policy.cycle_warn:
            return LoopSignal.WARNING
        return LoopSignal.OK

    # ==================================================================
    # 条件边
    # ==================================================================
    def _route_after_tool(self, state: AgentState) -> str:
        """决定「再来一步」还是「收工」。

        三条规则，顺序即优先级：

        1. **只有 ``COMPLETED`` 才允许继续**。``FAILED`` 是终态（不重试，§35 由平台裁决）；
           ``WAITING_*`` 说明图正挂着，此时路由到 ``tool`` 是安全的 ——
           挂起的任务本来就要回到 ``tool`` 节点被重跑。
        2. ``loop_signal == STOP`` -> 收工（§32）。循环检测必须真的改变路由，
           否则它只是一条日志。
        3. 步数上限：``step_index`` 走完 ``plan``，或 ``execution_path``
           达到 ``max_steps``，都收工。第二道闸门防的是「计划只有 2 步但
           因为恢复被反复重入」这类意外。
        """
        status = state.get("status")

        if status in ("WAITING_TOOL", "WAITING_HUMAN", "PLANNING", "RUNNING", "INIT"):
            # 挂起/未完成：任务停在 tool 节点上，恢复时要回到同一个节点重跑。
            return "tool"

        if status == "COMPLETED":
            if state.get("loop_signal") == LoopSignal.STOP.value:
                return "end"
            if int(state.get("step_index", 0)) >= len(state.get("plan") or []):
                return "end"
            if len(state.get("execution_path") or []) >= int(state.get("max_steps", 6)):
                return "end"
            return "tool"

        # FAILED / STOPPED / 其它未知取值：一律收工，绝不带着错误状态继续推进。
        return "end"

    # ==================================================================
    # 对外驱动
    # ==================================================================
    def run(
        self,
        request: str,
        *,
        run_id: str,
        agent_id: str = "agent_01",
        tenant_id: str = "tenantA",
        max_steps: int = 6,
    ) -> dict:
        """跑一次新任务；遇到 ``interrupt()`` 就**原地返回**，不阻塞等待。

        返回的 dict 至少含 ``{"run_id", "status", "steps", "tool_results",
        "execution_path", "messages"}``。``status`` 为 ``WAITING_TOOL`` /
        ``WAITING_HUMAN`` 时表示图已经挂起并把资源让出了（§49），
        调用方应当稍后用 :meth:`resume` 回来接着走 —— 而不是在这里 ``sleep``。

        :param request: 用户原始请求（自然语言）。它进 ``messages[0]``，
            规划器从这里读需求。
        :param run_id: 本次运行的 ID。它同时是幂等键的 ``workflow_run_id``（§7）
            与 Checkpoint 线程名（§18）—— **同一个 run_id 必须贯穿两处**，
            否则恢复时算出的幂等键与首次提交不同，防重直接失效。
        :param max_steps: 本图最多执行多少步，写进 State 一起进 Checkpoint。
        """
        graph = self.graph
        state = initial_state(
            run_id=run_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            request=request,
            max_steps=max_steps,
        )
        config = self.checkpoint.graph_config(run_id)

        logger.info("run=%s 启动: %s", run_id, request)
        result = graph.invoke(state, config)
        return self._view(run_id, result)

    def resume(
        self,
        *,
        run_id: str,
        decision: Optional[str] = None,
        arguments: Optional[dict] = None,
    ) -> dict:
        """把挂起的图叫醒（§49 / §52）。

        两种挂起状态，两种叫醒方式 —— 但对调用方是同一个入口：

        * ``WAITING_TOOL``：先 ``gateway.status(call_id)`` 看一眼。
          **还没出终态就直接返回、不推进图**：把图叫醒了却发现 Tool 还在跑，
          只会让它立刻再挂一次，白白多写一个 Checkpoint。所以这里用
          「状态查询」而不是「阻塞等待」做闸门。
          出终态了才 ``Command(resume=...)`` —— 节点重跑时会拿同一个幂等键重提交，
          平台判 ``DEDUPLICATED`` 并给出结果。
        * ``WAITING_HUMAN``：把人工决定写进 State（``update_state``）**再**唤醒。
          顺序不能反：节点重跑时会**先**读 ``state["human_decision"]``，
          读到才走 APPROVE / REJECT / MODIFY，读不到就原地再挂一次。

        :param decision: ``APPROVE`` / ``REJECT`` / ``MODIFY``（§52）。
        :param arguments: ``MODIFY`` 时人工改写后的参数。
        """
        snapshot = self.state(run_id)
        if snapshot is None:
            return {
                "run_id": run_id,
                "status": "NOT_FOUND",
                "reason": "该 run_id 没有 Checkpoint 记录，无法恢复",
                "steps": [],
                "tool_results": {},
                "execution_path": [],
                "messages": [],
            }

        status = snapshot.get("status")
        values = snapshot.get("values") or {}
        config = self.checkpoint.graph_config(run_id)
        graph = self.graph

        if status == "WAITING_HUMAN":
            if not decision:
                # 没有决定就叫醒 = 让它再挂一次，纯粹浪费一次写。直接原样返回。
                logger.info("run=%s 仍在 WAITING_HUMAN，等待人工决策", run_id)
                return snapshot_to_view(run_id, snapshot)
            graph.update_state(
                config,
                {
                    "human_decision": str(decision).upper(),
                    "human_arguments": arguments,
                    # retry_count 在这里清零没有意义（节点自己会 +1），
                    # 保留原值让 §75 的「不无限重试」闸门能看到真实次数。
                },
                # as_node 必须显式指定为挂起的那个节点：不指定的话 LangGraph 会
                # 去猜「这是谁写的」，猜错就会把 next 重算成别的节点，
                # 于是恢复跑到一半从错误的节点开始。
                as_node="tool",
            )
            logger.info("run=%s 人工决策 %s，恢复执行", run_id, decision)
            result = graph.invoke(
                Command(resume={"decision": str(decision).upper(), "arguments": arguments}),
                config,
            )
            return self._view(run_id, result)

        if status == "WAITING_TOOL":
            pending = values.get("pending_tool_call") or {}
            call_id = pending.get("call_id")
            ready, reason = self._tool_ready(call_id)
            if not ready:
                logger.info("run=%s Tool %s 仍未出终态（%s），保持挂起", run_id, call_id, reason)
                view = snapshot_to_view(run_id, snapshot)
                view["reason"] = reason
                return view
            logger.info("run=%s Tool %s 已出终态，恢复执行", run_id, call_id)
            result = graph.invoke(
                Command(resume={"call_id": call_id, "ready": True, "reason": reason}),
                config,
            )
            return self._view(run_id, result)

        # 不是挂起状态：可能是已经跑完，也可能是还在跑（不该发生）。原样返回即可。
        logger.info("run=%s 当前 status=%s，无需 resume", run_id, status)
        return snapshot_to_view(run_id, snapshot)

    def state(self, run_id: str) -> Optional[dict]:
        """读该 run 的当前 Checkpoint 快照；没有则 ``None``。

        返回结构见 :meth:`CheckpointManager.snapshot` —— 顶层**同时**有
        ``status``（快捷方式）与 ``values``（完整 State），
        因为「这图停在哪」是最常问的问题，不该每次都从 values 里挖。
        """
        return self.checkpoint.snapshot(self.graph, run_id)

    def pending_call(self, run_id: str) -> Optional[dict]:
        """取出该 run 的 ``pending_tool_call``，没有则 ``None``。

        **请一律用这个方法，不要自己写 ``state(run_id)["pending_tool_call"]``。**
        它埋在 ``values`` 下面（``{"status": ..., "values": {...}}``），
        从顶层直接取会静默拿到 ``None`` —— 而 ``None`` 会被恢复流程理解成
        「这笔调用从未提交过，可以安全重提交」，对 NON_IDEMPOTENT 的 Tool
        就是第二次真实副作用。

        恢复流程需要的信息都在 `:mod:`app.agent.recovery`` 里消费，
        这里只负责把「挖错层级」这个坑堵上。
        """
        snapshot = self.state(run_id)
        if not snapshot:
            return None
        values = snapshot.get("values") or {}
        return values.get("pending_tool_call")

    def history(self, run_id: str, *, limit: Optional[int] = None) -> list[dict]:
        """该 run 的每一步 Checkpoint（§18「每一步执行 -> Checkpoint」的可视化）。"""
        return self.checkpoint.history(self.graph, run_id, limit=limit)

    def close(self) -> None:
        """释放自己创建的 checkpoint 连接；外部传入的 checkpoint 不代为关闭。"""
        if self._owns_checkpoint:
            self.checkpoint.close()

    def __enter__(self) -> "AgentRuntime":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ==================================================================
    # 内部辅助
    # ==================================================================
    def _tool_ready(self, call_id: Optional[str]) -> tuple[bool, str]:
        """查一次 Tool 是否已出终态。返回 ``(是否可推进, 原因)``。

        异常一律当作「不可推进」：网关查询失败时贸然唤醒图，
        节点会拿着一个未知状态去分支，后果比「多等一轮」严重得多。
        """
        if not call_id:
            return False, "状态里没有 pending_tool_call.call_id"
        try:
            submit = self.gateway.query(call_id)
        except Exception as exc:
            return False, f"查询 Tool 状态失败: {exc}"

        status = submit.status
        if status.is_pending:
            return False, f"Tool 仍在 {status.value}"
        if submit.is_final or status in (
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.RECOVERY_REQUIRED,
        ):
            return True, f"Tool 已到 {status.value}"
        # 其余（WAITING_HUMAN 等）不自动推进：那是要人来决定的
        return False, f"Tool 状态 {status.value} 需人工确认"

    def _view(self, run_id: str, result: Optional[dict] = None) -> dict:
        """把图的一次调用结果压成统一的可打印视图。

        ``invoke()`` 的返回值有两个特点要处理：

        1. 挂起时它带一个 ``__interrupt__`` 键（langgraph 的约定），
           里面的值是 :class:`~langgraph.types.Interrupt` 对象，不可直接打印。
        2. 挂起时节点的返回值**没有**被写进 State（``interrupt()`` 抛异常，
           函数提前退出），所以 ``status`` / ``pending_tool_call``
           还停留在挂起之前的样子。

        第 2 点必须补：否则「图停在 WAITING_TOOL」这个事实只存在于
        ``__interrupt__`` 里，而 :meth:`state` 读回来却是 ``PLANNING``——
        恢复流程会据此做出错误判断。补法是 ``update_state()`` 把
        「我正在等什么」显式写进 Checkpoint，让挂起状态**对读者可见**。
        """
        result = result or {}
        interrupts = result.get("__interrupt__") or []

        if interrupts:
            payload = _interrupt_payload(interrupts[0])
            kind = str(payload.get("type") or "WAITING_TOOL")
            pending = _pending_from_interrupt(payload)
            self._record_wait(run_id, kind, pending, payload)
            snapshot = self.state(run_id) or {"values": result}
            view = snapshot_to_view(run_id, snapshot)
            view["interrupt"] = payload
            return view

        snapshot = self.state(run_id) or {"values": result}
        return snapshot_to_view(run_id, snapshot)

    def _record_wait(
        self, run_id: str, kind: str, pending: Optional[dict], payload: dict
    ) -> None:
        """把「正在等什么」写进 Checkpoint（见 :meth:`_view` 的说明）。

        写失败不抛异常：这是**增强可观测性**的写入，不是正确性的必要条件
        （真正的正确性由 ``__interrupt__`` 里那份载荷保证，它已经落盘了）。
        为了一个展示字段把恢复流程搞崩，不值得。
        """
        try:
            self.graph.update_state(
                self.checkpoint.graph_config(run_id),
                {
                    "status": kind,
                    "pending_tool_call": pending,
                    "current_step": f"tool:{payload.get('tool_name')}",
                    "checkpoint_reason": (
                        "awaiting_human"
                        if kind == "WAITING_HUMAN"
                        else "tool_accepted"
                    ),
                },
                as_node="tool",
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("写入挂起状态失败 run=%s: %s", run_id, exc)


# ======================================================================
# 视图与载荷工具
# ======================================================================
def snapshot_to_view(run_id: str, snapshot: dict) -> dict:
    """State 快照 -> :meth:`AgentRuntime.run` / :meth:`resume` 的返回契约。

    六个键是刻意的：``steps`` 让「走到第几步」可见，``execution_path`` 让
    §31 循环检测的输入可见，``messages`` 让「Agent 说了什么」可见。
    后三个（``status`` / ``tool_results`` / ``execution_path``）在挂起时也必须能读到，
    否则调用方无法判断该 resume 还是该放弃。
    """
    values = (snapshot or {}).get("values") or {}
    return {
        "run_id": run_id,
        "status": snapshot.get("status") or values.get("status"),
        "steps": [dict(step) for step in (values.get("plan") or [])],
        "tool_results": dict(values.get("tool_results") or {}),
        "execution_path": list(values.get("execution_path") or []),
        "messages": list(values.get("messages") or []),
        "pending_tool_call": values.get("pending_tool_call"),
        "error": values.get("error"),
        "loop_signal": values.get("loop_signal"),
        "human_decision": values.get("human_decision"),
        "next": list(snapshot.get("next") or []),
        "summary": summarize(values),
    }


def _interrupt_payload(raw: Any) -> dict:
    """把 langgraph 的 :class:`Interrupt` 对象/裸 dict 统一成 dict。"""
    value = getattr(raw, "value", raw)
    if isinstance(value, dict):
        return value
    return {"type": "WAITING_TOOL", "raw": value}


def _pending_from_interrupt(payload: dict) -> Optional[dict]:
    """从 interrupt 载荷还原出要写回 State 的 ``pending_tool_call``。

    ``interrupt()`` 里带上 ``idempotency_key`` 与 ``arguments`` 不是冗余：
    没有它们，回复流程就只能去查库反推「刚才提交了什么」，
    而那在崩溃场景下正是查不到的东西。
    """
    call_id = payload.get("call_id")
    if not call_id:
        return None
    # 节点已经把 §18 的 PendingToolCall 塞进载荷了，优先用它；
    # 兜底路径是为了兼容「只写了 call_id」的手工构造载荷（演示脚本常见）。
    pending = payload.get("pending")
    if isinstance(pending, dict) and pending.get("call_id"):
        return dict(pending)
    return {
        "call_id": call_id,
        "idempotency_key": payload.get("idempotency_key"),
        "tool_name": payload.get("tool_name"),
        "arguments": payload.get("arguments") or {},
        "submitted_at": payload.get("submitted_at"),
        "attempt": payload.get("attempt", 0),
    }


def _normalize_decision(raw: Any) -> Optional[str]:
    """把 resume 载荷里的决定字段抽出来（``{"decision": ...}`` 或裸字符串都认）。

    为什么要宽容两种形状：``interrupt()`` 的 resume 值由**调用方**决定，
    手工推演时人往往直接传 ``"APPROVE"``，而 :meth:`AgentRuntime.resume`
    传的是 ``{"decision": ..., "arguments": ...}``。两种都接受，
    免得演示时因为一个信封格式卡住。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("decision", "human_decision", "action"):
            if raw.get(key):
                return str(raw[key])
    return None
