"""Checkpoint —— Agent 执行进度的持久化（说明书 §18 / §20）。

§20 的核心论点：Checkpoint 与 Idempotency 解决的是**两个不同问题**
-------------------------------------------------------------------
这是整章最容易混淆、也最容易出事故的地方。一句话对照：

============================ ============================== ================================
维度                          Checkpoint（本模块，§18）       Idempotency（§7-§12）
============================ ============================== ================================
管什么                       **Agent 执行进度**              **Tool 不重复执行**
存在哪里                     LangGraph 的 SQLite/Postgres     Redis 幂等键 + PostgreSQL 事实表
键是什么                     ``thread_id = run:<run_id>``     ``idempotency_key``（§7 配方）
回答的问题                   「我走到第几步了？」              「这笔操作是不是已经做过了？」
失效后会发生什么              重新规划，可能换个计划执行        重复扣款 / 重复建单 / 重复发消息
单独使用的后果                Agent 不重复跑，但 Tool 会被重复提交
单独使用的后果（另一侧）        Tool 不重复执行，但 Agent 不知道结果在哪、要不要重跑
============================ ============================== ================================

用两个反例把这张表钉死：

* **只有 Checkpoint 会怎样**：Agent 在第 3 步提交了 ``payment.charge`` 之后崩溃。
  Checkpoint 忠实记录了「第 3 步已提交」，重启后 CP 从第 4 步继续 —— 看起来没问题。
  但如果第 3 步提交时崩溃点发生在「Tool 已执行、Checkpoint 还没写」之间，
  恢复时它会把第 3 步**再提交一次**：第二次扣款。Checkpoint 救不了这个，
  因为它的粒度是「整张图的状态」，而不是「这笔副作用做过没有」。
* **只有 Idempotency 会怎样**：幂等键保证 ``payment.charge`` 只扣一次，很好。
  但 Agent 重启后手里是一张白纸：计划是什么？走到第几步了？上一次的
  ``call_id`` 是多少（不拿着它，连回查结果的钥匙都没有）？只能从头再来一遍，
  而重来的那一次会命中幂等键、拿到 ``DEDUPLICATED`` —— 结果对了，但中间的
  每一步都要重新消耗一次 LLM 调用与 Tool 提交，成本和延迟都翻倍。

**结论（§20 的落点）：Checkpoint + Idempotency 必须一起用。**
本模块负责前者，幂等由 :mod:`app.gateway` / :mod:`app.idempotency` 负责，
两者在 :mod:`app.agent.recovery` 里汇合 —— 那正是 §19/§55 那张恢复分支图的实现。

实现细节上的一个坑
------------------
``SqliteSaver.from_conn_string()`` 是个 ``@contextmanager``：它的返回值只在 ``with``
块内有效，出了块连接就被关掉。而 :class:`CheckpointManager` 的生命周期是「整个
AgentRuntime 存活期间」，两者对不上。所以这里用
``sqlite3.connect(path, check_same_thread=False)`` 自己建连接再交给
``SqliteSaver(conn)``——**``check_same_thread=False`` 不是可选项**：
``SqliteSaver`` 会把连接交给连接池，不加这个参数会在多线程下直接抛异常。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from langgraph.checkpoint.sqlite import SqliteSaver

logger = logging.getLogger(__name__)


@dataclass
class CheckpointConfig:
    """Checkpoint 的存放位置与线程命名。

    :param db_path: SQLite 文件路径。用 ``:memory:`` 时进程退出即丢 ——
        演示「崩溃重启后 checkpoint 还在不在」必须给一个真实文件路径。
    :param thread_prefix: 线程名前缀。**为什么要前缀**：``run_id`` 与
        ``thread_id`` 是两个命名空间，同一个 SQLite 库里可能还躺着别的图
        （比如 :mod:`app.agent.graph` 的多个 Agent 版本）的 checkpoint，
        加前缀能在 ``list_threads()`` 时一眼区分来源，也避免和别的键撞上。
    :param extra: 保留给调用方的附加配置（演示脚本放备注用），不参与逻辑。
    """

    db_path: str = "checkpoints.sqlite"
    thread_prefix: str = "run"
    extra: dict[str, Any] = field(default_factory=dict)


class CheckpointManager:
    """LangGraph Checkpoint 的门面：连接管理 + 线程命名 + 快照读取。

    **线程安全**：``SqliteSaver`` 内部对连接做了池化与加锁，同一实例可以被
    多个线程/多个 AgentRuntime 共享；但「一个 CheckpointManager 一个连接」，
    不要用同一个 db_path 建多个 manager 去并发写（SQLite 的写锁会互相阻塞）。
    """

    def __init__(self, config: Optional[CheckpointConfig] = None) -> None:
        self.config = config or CheckpointConfig()
        self._closed = False

        # 为什么不用 SqliteSaver.from_conn_string()：
        # 它是 contextmanager，yield 之后连接就被 closing() 关掉了，
        # 而我们需要这条连接活到 close() 为止。所以自己建、自己关。
        self._conn = sqlite3.connect(
            self.config.db_path,
            check_same_thread=False,
        )
        self._saver = SqliteSaver(self._conn)

        # 建表：SqliteSaver 默认懒建表，第一次 put 时才创建。
        # 这里主动 setup()，好处是「空库也能被 list_threads() 安全查询」，
        # 排障时不会出现「表不存在」这种与业务无关的噪音异常。
        try:
            self._saver.setup()
        except Exception as exc:  # pragma: no cover - 只可能在磁盘/权限异常时触发
            logger.warning("SqliteSaver.setup() 失败（后续 put 时会重试）: %s", exc)

        logger.debug("checkpoint manager ready: %s", self.config.db_path)

    # ==================================================================
    # 基础设施
    # ==================================================================
    def saver(self) -> SqliteSaver:
        """给 ``builder.compile(checkpointer=...)`` 用的 saver 实例。

        写成方法而不是属性，是为了让「这里拿到的是一个活着的对象」这件事
        在调用点显式可见 —— 传进 ``compile()`` 之后 saver 的生命周期就跟着图走了，
        ``close()`` 之前不要提前关掉底层连接。
        """
        return self._saver

    @property
    def db_path(self) -> str:
        """当前 checkpoint 落盘的路径（演示与排障时打印用）。"""
        return self.config.db_path

    def thread_id(self, run_id: str) -> str:
        """``run_id`` -> LangGraph 的 ``thread_id``（``run:<run_id>``）。

        为什么要有这层映射而不是直接用 ``run_id``：同一个 run 在恢复时
        必须落到**同一个 thread** 上，而这个映射规则一旦写死在两处就容易写歪
        （一处 ``run:`` 一处 ``run-``，结果恢复时读到一个空 thread，然后「从头重跑」）。
        收敛到这一个方法，两边都调它。
        """
        return f"{self.config.thread_prefix}:{run_id}"

    def graph_config(self, run_id: str) -> dict:
        """LangGraph 的 ``config`` 参数：``{"configurable": {"thread_id": ...}}``。"""
        return {"configurable": {"thread_id": self.thread_id(run_id)}}

    def close(self) -> None:
        """释放连接。可重复调用（幂等），方便在 ``finally`` 里无脑调。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except Exception as exc:  # pragma: no cover
            logger.warning("关闭 checkpoint 连接失败: %s", exc)

    def __enter__(self) -> "CheckpointManager":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ==================================================================
    # 读快照
    # ==================================================================
    def snapshot(self, graph: Any, run_id: str) -> Optional[dict]:
        """读当前 State 快照，转成可打印的 dict；thread 不存在时返回 ``None``。

        返回结构（``values`` 之外的键都是 LangGraph 的元信息）::

            {
              "run_id": str,
              "thread_id": str,
              "status": str | None,          # values.status 的快捷方式
              "values": dict,                # 完整 State
              "next": [str, ...],            # 下一步会进哪个节点；空 = 已结束
              "interrupts": [ {...} ],       # 挂起中的 interrupt（人工审批/等待 Tool）
              "checkpoint_id": str | None,
              "created_at": str | None,
              "step": int | None,
              "source": str | None,
            }

        ``status`` 单独提一层出来，是因为「这图现在停在哪」是恢复流程最先要问的问题，
        每次都要写 ``snap["values"]["status"]`` 太啰嗦且容易在 values 为空时炸掉。
        """
        config = self.graph_config(run_id)
        try:
            state = graph.get_state(config)
        except Exception as exc:  # pragma: no cover - 线程不存在 / 库损坏
            logger.warning("读取 checkpoint 失败 run_id=%s: %s", run_id, exc)
            return None

        values = getattr(state, "values", None)
        if not values:
            # values 为空 = 这个 thread 从来没有被写过 checkpoint。
            # 返回 None 而不是空 dict，让调用方能用 `is None` 区分
            # 「没跑过」和「跑过但状态为空」——恢复流程对这两者的处置不同。
            return None

        snapshot = getattr(state, "config", None) or {}
        metadata = getattr(state, "metadata", None) or {}
        checkpoint_id = None
        if isinstance(snapshot, dict):
            checkpoint_id = (snapshot.get("configurable") or {}).get("checkpoint_id")

        return {
            "run_id": run_id,
            "thread_id": self.thread_id(run_id),
            "status": values.get("status"),
            "values": _jsonable(values),
            "next": list(getattr(state, "next", ()) or ()),
            "interrupts": [_jsonable(i) for i in (getattr(state, "interrupts", ()) or ())],
            "checkpoint_id": checkpoint_id,
            "created_at": _iso(getattr(state, "created_at", None)),
            "step": metadata.get("step"),
            "source": metadata.get("source"),
        }

    def history(self, graph: Any, run_id: str, *, limit: Optional[int] = None) -> list[dict]:
        """该 thread 的**每一步**快照 —— §18「每一步执行 -> Checkpoint」的可视化。

        返回按时间**从新到旧**排列（LangGraph ``get_state_history`` 的顺序），
        每条含 ``checkpoint_id`` / ``next`` / ``status`` / ``step`` / ``source``。

        :param limit: 只取最近 N 条。默认全取 —— 演示里一张图也就十几步。
            生产里别这么干，LangGraph 会把整条 checkpoint 链都读出来。
        """
        config = self.graph_config(run_id)
        out: list[dict] = []
        try:
            for state in graph.get_state_history(config):
                metadata = getattr(state, "metadata", None) or {}
                values = getattr(state, "values", None) or {}
                snapshot_meta = getattr(state, "config", None) or {}
                checkpoint_id = None
                if isinstance(snapshot_meta, dict):
                    checkpoint_id = (snapshot_meta.get("configurable") or {}).get(
                        "checkpoint_id"
                    )
                out.append(
                    {
                        "checkpoint_id": checkpoint_id,
                        "created_at": _iso(getattr(state, "created_at", None)),
                        "step": metadata.get("step"),
                        "source": metadata.get("source"),
                        "next": list(getattr(state, "next", ()) or ()),
                        "status": values.get("status"),
                        "step_index": values.get("step_index"),
                        "pending": bool(values.get("pending_tool_call")),
                        "interrupts": len(getattr(state, "interrupts", ()) or ()),
                    }
                )
                if limit is not None and len(out) >= limit:
                    break
        except Exception as exc:  # pragma: no cover
            logger.warning("读取 checkpoint 历史失败 run_id=%s: %s", run_id, exc)
        return out

    def list_threads(self) -> list[str]:
        """列出库里已有的 thread_id（按最近写入排序）。

        直接查 ``checkpoints`` 表而不是遍历 saver 的内存结构：
        我们要的是「**落盘的**有哪些」，那正是崩溃恢复时能用的集合。
        """
        try:
            rows = self._conn.execute(
                "SELECT thread_id, MAX(rowid) AS last_row FROM checkpoints "
                "GROUP BY thread_id ORDER BY last_row DESC"
            ).fetchall()
        except sqlite3.Error as exc:
            # 表还没建（空库 + setup 失败）时不该炸掉调用方
            logger.debug("list_threads 查询失败（可能表不存在）: %s", exc)
            return []
        return [row[0] for row in rows]


# ======================================================================
# 内部工具
# ======================================================================
def _jsonable(value: Any) -> Any:
    """把任意值压成可打印/可 JSON 序列化的形态。

    State 里理论上全是 JSON-safe 的值，但 ``ResultEnvelope`` / ``ToolCall`` 这类
    Pydantic 对象一旦被顺手塞进来就会破坏这个假设。这里做一次「兜底降级」：
    有 ``model_dump`` 就用它，否则退回 ``str``——**宁可打印得难看，也不要抛异常**
    （快照读取失败发生在恢复路径上，那是最不该再炸一次的地方）。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump(mode="json"))
        except Exception:  # pragma: no cover
            return str(value)
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
