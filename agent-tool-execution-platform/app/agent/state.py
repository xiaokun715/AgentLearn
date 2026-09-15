"""Agent State —— LangGraph 图里流动的状态契约（说明书 §47）。

为什么 State 要单独成一个文件
-----------------------------
§47 的论点：Agent 的「记忆」不是一个进程内的全局变量，而是一份**可序列化、
可检查点、可恢复**的数据结构。把它抽成 :class:`AgentState` 换来三件事：

1. **可 Checkpoint**（§18）：LangGraph 每一步都会把整份 State 落盘，
   因此 State 里的每个字段都必须是 JSON-safe 的（dict / list / str / int / bool）。
2. **可恢复**（§19）：崩溃重启后，恢复流程只需要 ``pending_tool_call`` 与 ``status``
   两个字段，就足以判断该 WAIT、该取结果、还是该重新提交。
3. **可观测**：:func:`summarize` 把整份 State 压成一个紧凑视图，
   演示时直接打印就能看懂「Agent 走到哪了」。

累加语义（reducer）—— 本文件最容易踩的坑
-----------------------------------------
标注了 ``Annotated[..., operator.add]`` 的字段**不是覆盖，而是拼接追加**：

========================== ==================== ==========================================
字段                        语义                 后果
========================== ==================== ==========================================
``messages``                ``operator.add``     节点返回的 list 会**接在已有值后面**
``execution_path``          ``operator.add``     同上；只增不减，永远不会被清空
其它字段                     覆盖（LangGraph 默认）  节点返回什么，状态里就是什么
========================== ==================== ==========================================

``execution_path`` 「只增不减」正是 §31 循环检测能工作的前提：它要看的就是
「历史上依次走过哪些 Tool」。如果每步都被覆盖，就没有历史可看，循环检测也就无从谈起。
反过来说，写节点时**不要**在里面手工拼接 ``state["messages"] + [新消息]``——
LangGraph 已经替你拼了，再拼一次就会出现重复消息。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Optional, TypedDict

# 计划里单个步骤最多允许携带的字段（仅用于 docstring 说明，不参与运行时校验）
STEP_KEYS = ("step_id", "tool_name", "arguments", "status", "call_id")


class AgentState(TypedDict, total=False):
    """Agent 图的完整状态（§47）。

    ``total=False``：所有字段都是可选的。这不是偷懒，而是为了让「半成品状态」
    也能被 Checkpoint 下来 —— LangGraph 在第一个节点跑完之前，
    State 里只有初始化的那几个字段，若强制要求全字段存在就根本无法启动。

    ---------------------------------------------------------------
    §47 原样字段
    ---------------------------------------------------------------
    :param messages: 对话/事件流。**累加语义**。约定每条形如
        ``{"role": "user" | "assistant" | "tool", "content": str, ...}``。
        用户请求就是 ``messages[0]``——规划器从这里取原始需求，
        因此不需要额外开一个 ``request`` 字段。
    :param current_step: 当前正在执行的步骤标识（``planner`` / ``tool`` /
        ``tool:<name>``），用于日志与调试时一眼看出「卡在哪」。
    :param pending_tool_call: 已经提交、但还没拿到终态的那次调用（§18）。
        结构等价于 :class:`~app.domain.models.PendingToolCall` 的 JSON 形式：
        ``{call_id, idempotency_key, tool_name, arguments, submitted_at, attempt}``。
        崩溃恢复的入口就是它 —— 有它就说明「有一次 Tool 已经出去了，结果未知」。
    :param tool_results: ``{call_id: {...}}``。每个已完成的调用在这里留一条归一化结果，
        键是 ``call_id`` 而不是 ``tool_name``：同一个 Tool 在一张图里可能被调用多次，
        用名字做键会互相覆盖，而且丢掉「哪一次」这个信息。
    :param retry_count: 已经重试过的次数。**必须有**，否则一次永久性故障会变成
        永不停止的重放（§75「不无限 Retry」）。人工审批通过后的再次提交也计在这里。
    :param execution_path: **累加语义**。每一步的 ``tool_name`` 按执行顺序推进来，
        供 §31 循环检测消费（``A -> B -> A -> B`` 超过阈值即升级处置）。
    :param status: Agent 侧的状态机取值，见 :data:`AGENT_STATUSES`。
        注意它与 :class:`~app.domain.enums.ExecutionStatus` 不是一回事：
        后者描述**一次 Tool 执行**，前者描述**一整张 Agent 图**。

    ---------------------------------------------------------------
    平台补充字段
    ---------------------------------------------------------------
    :param run_id: 这次 Agent 运行的 ID。它同时是幂等键的 ``workflow_run_id``
        分量（§7）与 Checkpoint 的 thread 标识（§18），两者必须同源。
    :param agent_id: 发起方身份，权限校验（§26）与审计（§42）都用它。
    :param tenant_id: 租户，幂等键的第一分量（§7）——不同租户的同名参数不能互相串结果。
    :param plan: 待执行的步骤计划，``[{step_id, tool_name, arguments}, ...]``。
        规划一次、执行多次：计划本身也进 Checkpoint，所以恢复后不必重新规划
        （重新规划可能得出不同的计划，那会让「恢复」变成「重来」）。
        执行过程中本字段会被逐步**回填** ``call_id`` / ``status``，成为步骤台账，
        :meth:`AgentRuntime.run` 的返回值里的 ``steps`` 就是它。
    :param step_index: 下一个要执行的 ``plan`` 下标。恢复时靠它知道「走到第几步」。
    :param human_decision: 人工审批结论：``APPROVE`` / ``REJECT`` / ``MODIFY``（§52）。
        ``None`` 表示还没人做决定 —— 节点看到它为空就应该 ``interrupt()`` 等人。
    :param human_arguments: ``MODIFY`` 时人工改写后的参数。**为什么单独放一个字段**：
        原始参数要留在 ``plan`` 里做审计（「人改了什么」本身是要留痕的事实），
        改写后的参数进 ``pending_tool_call.arguments`` 去执行。
    :param error: 终态失败的结构化描述 ``{error_type, message, tool_name, ...}``。
        存 dict 而不是字符串，是为了让「错误分类」（§34）能被下游直接消费。
    :param loop_signal: §32 循环检测的当前档位（``OK`` / ``WARNING`` /
        ``DEGRADED`` / ``STOP``）。它写在 State 里而不是只打日志，因为
        ``STOP`` 必须真的改变图的路由行为，而不只是留下一条日志。
    :param checkpoint_reason: 最近一次「为什么会停在这里」的一句话。
        ``run_started`` / ``tool_accepted`` / ``awaiting_human`` / ``tool_failed`` …
        排障时最想知道的就是这个，而它无法从其它字段反推出来。
    :param max_steps: 本张图最多执行多少步。**这是对 §47 字段清单的补充**：
        ``run(max_steps=...)`` 是运行时参数，而节点函数拿不到调用栈，
        只能从 State 里读，所以它必须随 State 一起进 Checkpoint
        （否则恢复之后步数上限就丢了，恢复出来的图可能比原计划跑得更久）。
    """

    # ---- §47 原样 ----
    messages: Annotated[list[dict], operator.add]
    current_step: str
    pending_tool_call: Optional[dict]
    tool_results: dict
    retry_count: int
    execution_path: Annotated[list[str], operator.add]
    status: str

    # ---- 平台补充 ----
    run_id: str
    agent_id: str
    tenant_id: str
    plan: list[dict]
    step_index: int
    human_decision: Optional[str]
    human_arguments: Optional[dict]
    error: Optional[dict]
    loop_signal: Optional[str]
    checkpoint_reason: str
    max_steps: int


# ======================================================================
# Agent 图自己的状态机取值
# ======================================================================
AGENT_STATUSES: tuple[str, ...] = (
    "INIT",           # 刚构造，还没规划
    "PLANNING",       # 规划完成，准备执行第一步
    "RUNNING",        # 正在执行某一步
    "WAITING_TOOL",   # 异步 Tool 已提交，等 Tool Event / 轮询（§49）
    "WAITING_HUMAN",  # 高风险拦截，等人工审批（§52）
    "COMPLETED",      # 全部步骤走完，结果齐全
    "FAILED",         # 终态失败（含 REJECTED），**不再重试**
    "STOPPED",        # 循环检测 STOP，主动收手（§32）
)
"""Agent 层状态取值。

**为什么要和** :class:`~app.domain.enums.ExecutionStatus` **分开**：
一张图里可能提交 5 次 Tool，每次都有自己的 ``ExecutionStatus``；
若把两者混用一个字段，就会出现「第 3 步在 PROCESSING，那整张图算什么」这类
无法回答的问题。图层与调用层各有一套状态机，靠 ``pending_tool_call`` 相互引用。
"""


def initial_state(
    *,
    run_id: str,
    agent_id: str,
    tenant_id: str,
    request: str,
    max_steps: int = 6,
) -> AgentState:
    """构造一张图的初始状态。

    三条刻意的设计：

    1. **累加字段必须显式给空 list**。``messages`` / ``execution_path`` 用的是
       ``operator.add`` reducer，首次写入时 LangGraph 需要一个已存在的值来拼接；
       不给的话不同版本行为不一致，显式给 ``[]`` 最稳。
    2. **``messages[0]`` 就是用户请求**。规划器从这里取需求（见
       :func:`app.agent.graph.default_planner`），因此不再单开 ``request`` 字段。
    3. **``status`` 从 ``INIT`` 起步**，``checkpoint_reason`` 说明为什么会有这份状态 ——
       「跑起来」和「恢复回来」在状态里长得一样，只有这一句能区分。
    """
    state: AgentState = {
        # §47 原样
        "messages": [{"role": "user", "content": request}],
        "current_step": "init",
        "pending_tool_call": None,
        "tool_results": {},
        "retry_count": 0,
        "execution_path": [],
        "status": "INIT",
        # 平台补充
        "run_id": run_id,
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "plan": [],
        "step_index": 0,
        "human_decision": None,
        "human_arguments": None,
        "error": None,
        "loop_signal": None,
        "checkpoint_reason": "run_started",
        "max_steps": max_steps,
    }
    return state


def request_of(state: AgentState) -> str:
    """从状态里取回最初的用户请求（``messages[0]`` 的 content）。"""
    for message in state.get("messages") or []:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def summarize(state: Optional[AgentState]) -> dict[str, Any]:
    """把整份 State 压成紧凑可打印视图 —— 演示时直接 ``print`` 就看得懂。

    刻意**不返回完整 messages / tool_results**：它们会随着执行不断变大，
    原样打印会把终端淹掉。真正要深挖时用 ``AgentRuntime.state(run_id)`` 拿原始快照。
    """
    if not state:
        return {"status": "EMPTY"}

    plan = state.get("plan") or []
    results = state.get("tool_results") or {}
    pending = state.get("pending_tool_call") or None

    return {
        "run_id": state.get("run_id"),
        "agent_id": state.get("agent_id"),
        "tenant_id": state.get("tenant_id"),
        "status": state.get("status"),
        "current_step": state.get("current_step"),
        "progress": f"{state.get('step_index', 0)}/{len(plan)}",
        "plan": [step.get("tool_name") for step in plan],
        "step_index": state.get("step_index", 0),
        "retry_count": state.get("retry_count", 0),
        "pending_tool_call": (
            {
                "call_id": pending.get("call_id"),
                "tool_name": pending.get("tool_name"),
                "idempotency_key": pending.get("idempotency_key"),
            }
            if pending
            else None
        ),
        "tool_result_count": len(results),
        "execution_path": list(state.get("execution_path") or []),
        "human_decision": state.get("human_decision"),
        "loop_signal": state.get("loop_signal"),
        "error": state.get("error"),
        "checkpoint_reason": state.get("checkpoint_reason"),
        "message_count": len(state.get("messages") or []),
    }
