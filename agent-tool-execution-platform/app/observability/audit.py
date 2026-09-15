"""审计事件门面 —— 说明书 §42 ``execution_event`` 的读写入口。

为什么审计事件是**一等公民**
----------------------------
§42 把 ``execution_event`` 和 ``tool_execution`` / ``tool_result`` 并列为平台的三张核心表。
它不是「顺手记点日志」：一次工具执行的**完整生命周期**（谁提交、被谁拒、命中幂等、
拿到租约、沙箱被杀、循环降级、转人工、恢复决策）都只能从这里回溯。
出问题时，``Agent 说它调了`` 和 ``数据库说它调了`` 之间往往就差在这张表上。

因此本模块做了两件事：

1. **统一事件名常量**（:class:`EventType`）。事件名一旦散落在各子系统里手写，
   拼写漂移（``tool.timeout`` vs ``tool_timeouted``）会让审计查询悄悄漏数据。
2. **统一的读写门面**。上层只依赖 :class:`AuditLog`，不直接拼 SQL。

写入路径走 ``db.append_event``（它自带事务）；``recent`` / ``search`` 需要
按时间/类型跨 call 查询，而这几个查询 :class:`~app.infra.database.Database`
没有提供，所以这里用 ``db.transaction()`` 拿连接自己 ``SELECT`` ——
**不改（也不该改）基础设施层**，门面自己保证查询与写入用的是同一个库。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..infra.database import Database
from ..domain.models import ExecutionEvent


class EventType:
    """全平台事件名常量（§42 的 ``event_type`` 取值域）。

    命名约定 ``<子系统>.<动作>``：前缀即「谁产生的」，便于按子系统切片查询。
    新增事件时**先在这里登记**，不要在调用点手写字符串。
    """

    # ---- 入口 / 网关 ----
    CALL_RECEIVED = "call.received"
    GATEWAY_REJECTED = "gateway.rejected"

    # ---- 参数校验与安全（§22-§25）----
    PARAMETER_REPAIRED = "parameter.repaired"
    INJECTION_DETECTED = "injection.detected"
    PERMISSION_DENIED = "permission.denied"

    # ---- 幂等（§8 / §10 / §12）----
    IDEMPOTENCY_HIT = "idempotency.hit"
    IDEMPOTENCY_CLAIMED = "idempotency.claimed"
    IDEMPOTENCY_COMPLETED = "idempotency.completed"

    # ---- 租约（§14 / §15 / §56）----
    LEASE_ACQUIRED = "lease.acquired"
    LEASE_RENEWED = "lease.renewed"
    LEASE_LOST = "lease.lost"
    LEASE_REAPED_RETRY = "lease.reaped.retry"
    LEASE_REAPED_HUMAN = "lease.reaped.human"

    # ---- 沙箱（§27-§29）----
    SANDBOX_CREATED = "sandbox.created"
    SANDBOX_KILLED = "sandbox.killed"

    # ---- 执行（§41）----
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_TIMEOUT = "tool.timeout"

    # ---- 循环检测（§30-§33）----
    LOOP_DUPLICATE = "loop.duplicate"
    LOOP_CYCLE = "loop.cycle"

    # ---- 人工介入（§51-§53）----
    HUMAN_REQUESTED = "human.requested"
    HUMAN_APPROVED = "human.approved"
    HUMAN_REJECTED = "human.rejected"
    HUMAN_MODIFIED = "human.modified"

    # ---- 恢复（§34-§36）----
    RECOVERY_DECIDED = "recovery.decided"

    # ---- Agent 侧（§18 / §55）----
    AGENT_CHECKPOINT = "agent.checkpoint"
    AGENT_RESUMED = "agent.resumed"

    #: 全部已知事件名 —— 供校验、文档、审计面板枚举使用。
    ALL: tuple[str, ...] = (
        CALL_RECEIVED,
        GATEWAY_REJECTED,
        PARAMETER_REPAIRED,
        INJECTION_DETECTED,
        PERMISSION_DENIED,
        IDEMPOTENCY_HIT,
        IDEMPOTENCY_CLAIMED,
        IDEMPOTENCY_COMPLETED,
        LEASE_ACQUIRED,
        LEASE_RENEWED,
        LEASE_LOST,
        LEASE_REAPED_RETRY,
        LEASE_REAPED_HUMAN,
        SANDBOX_CREATED,
        SANDBOX_KILLED,
        TOOL_STARTED,
        TOOL_COMPLETED,
        TOOL_FAILED,
        TOOL_TIMEOUT,
        LOOP_DUPLICATE,
        LOOP_CYCLE,
        HUMAN_REQUESTED,
        HUMAN_APPROVED,
        HUMAN_REJECTED,
        HUMAN_MODIFIED,
        RECOVERY_DECIDED,
        AGENT_CHECKPOINT,
        AGENT_RESUMED,
    )

    @classmethod
    def is_known(cls, event_type: str) -> bool:
        return event_type in cls.ALL


class AuditLog:
    """``execution_event`` 表的读写门面。

    :param db: 平台唯一事实来源（§44）
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # 写
    # ==================================================================
    def record(self, call_id: str, event_type: str, **payload: Any) -> None:
        """追加一条审计事件。

        ``**payload`` 会原样进 ``execution_event.payload``，因此必须是 JSON-safe 的
        （异常对象请用 ``exc.to_dict()`` 或 ``repr(exc)``，不要直接塞异常实例）。
        """
        self.db.append_event(call_id, event_type, dict(payload))

    # ==================================================================
    # 读
    # ==================================================================
    def events(self, call_id: str) -> list[ExecutionEvent]:
        """某个 call 的全部事件，按发生顺序（``seq`` 即数组下标）。"""
        return self.db.list_events(call_id)

    def event_types(self, call_id: str) -> list[str]:
        """某个 call 事件类型的**去重**序列 —— 一眼看出这次执行走到了哪几步。"""
        seen: list[str] = []
        for event in self.db.list_events(call_id):
            if event.event_type not in seen:
                seen.append(event.event_type)
        return seen

    def timeline(self, call_id: str) -> list[dict]:
        """排障用的时间线：``[{"seq", "event_type", "created_at", "payload"}, ...]``。

        ``seq`` 从 0 开始，是**事件在库里的顺序**（不是时间戳排序）——
        ``execution_event`` 的自增主键就是全平台的因果顺序，
        同一毫秒内的多条事件靠它才分得清先后。
        """
        return [
            {
                "seq": index,
                "event_type": event.event_type,
                "created_at": _iso(event.created_at),
                "payload": event.payload,
            }
            for index, event in enumerate(self.db.list_events(call_id))
        ]

    def recent(self, limit: int = 50) -> list[ExecutionEvent]:
        """最近的事件（**新的在前**）—— 运维大屏 / 排障首页用。"""
        return self._query(
            "SELECT * FROM execution_event ORDER BY id DESC LIMIT ?", (int(limit),)
        )

    def search(self, event_type: str, limit: int = 100) -> list[ExecutionEvent]:
        """按事件类型跨 call 查询（按发生顺序，最多 ``limit`` 条）。

        典型用法：``search(EventType.INJECTION_DETECTED)`` 拉出全部注入拦截记录，
        用于安全复盘。
        """
        return self._query(
            "SELECT * FROM execution_event WHERE event_type = ? ORDER BY id LIMIT ?",
            (event_type, int(limit)),
        )

    def count(self, event_type: Optional[str] = None) -> int:
        """事件计数（可按类型过滤）—— 轻量统计，不必把行拉回来。"""
        sql = "SELECT COUNT(*) AS n FROM execution_event"
        params: tuple = ()
        if event_type is not None:
            sql += " WHERE event_type = ?"
            params = (event_type,)
        with self.db.transaction() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row is not None else 0

    # ==================================================================
    # 内部
    # ==================================================================
    def _query(self, sql: str, params: tuple) -> list[ExecutionEvent]:
        """自己开事务查询（见模块 docstring：不动基础设施层）。

        ``try/finally`` 之外不需要额外回滚处理 —— ``Database.transaction()``
        已经保证了「异常回滚 + 锁释放」，这里只负责把行映射成模型。
        """
        with self.db.transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_event(row) for row in rows]


def _row_to_event(row: Any) -> ExecutionEvent:
    """``sqlite3.Row`` -> :class:`ExecutionEvent`。"""
    import json

    return ExecutionEvent(
        id=row["id"],
        call_id=row["call_id"],
        event_type=row["event_type"],
        payload=json.loads(row["payload"] or "{}"),
        created_at=_parse(row["created_at"]),
    )


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse(value: Optional[str]) -> datetime:
    """容错解析时间戳；脏数据不能让审计读取整个失败。"""
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)
