"""人工介入 / 审批 —— 说明书 §51 Human-in-the-loop / §52 状态机 / §53 审批。

为什么把「人」放进自动化的关键路径里
------------------------------------
§51 的判断很直接：**有些操作不该被自动执行**。
``database.delete`` / ``production.deploy`` 这类操作一旦做错就无法回滚，
让 Agent「自主决定」等于把不可逆的风险交给一个概率模型。

§52 的状态机（本模块的实现蓝本）::

    WAITING_HUMAN ──APPROVE──► 可继续执行（带原参数）
                  ──REJECT───► CANCELLED（终态，不再重试 —— 这一点很重要：
                                        人工拒绝不是「一次失败」，而是「不许做」）
                  ──MODIFY───► 走 Parameter Repair：
                                   modified_arguments 必须**重新过一遍校验**
                                   （Schema -> 业务约束 -> 权限），
                                   通过之后才执行

``MODIFY`` 为什么要重新校验
---------------------------
这是最容易写错、后果也最严重的一步。人会犯错、会手滑、也可能**根本不懂**
这个 Tool 的参数约束（比如把 ``timeout`` 改成 ``300000`` 秒）。
如果因为「这是人改的」就跳过校验直接执行，那么：

* §25 的确定性安全约束（路径白名单、命令白名单、区间）全部形同虚设；
* 一次审批动作就绕过了整条 §21-§26 的防线。

所以 ``MODIFY`` 的正确语义是「人提供了一个**新的候选参数**」，
它和 LLM 自愈产出的候选参数地位完全相同 —— 必须走同一套校验。

存储在哪儿
----------
审批单放 Redis（``approval:{call_id}``，§43 的协调层），因为它是**短生命周期的运行态**；
每一次状态迁移同时向 DB 追加 ``execution_event``（``human.*``），
因为「谁批的、什么时候批的、批的理由」是**必须长期留存的审计事实**（§42）。
两者缺一不可：只写 Redis，重启就没了；只写 DB，每次判定都要查库。

非法迁移一律抛错
----------------
对已经 ``APPROVED`` 的单子再 ``approve`` 是**状态机违规**，本实现抛 :class:`ValueError`
（而不是 ``HumanRejected``）：``HumanRejected`` 代表「人拒绝了」这一**业务结论**，
上层会据此走 ``CANCELLED``；而重复批准是调用方的时序错误，混进业务结论里
会让「这批操作被人工拒绝了多少次」这种统计变得不可信。
不存在的 ``call_id`` 则抛 :class:`~app.domain.errors.HumanRejected`：
「查不到审批单」在业务上等价于「没有拿到放行许可」，绝不能不报错地继续执行。
"""
from __future__ import annotations

from typing import Optional

from ..config import AppConfig
from ..domain.enums import RiskLevel
from ..domain.errors import HumanRejected
from ..domain.models import ApprovalRecord, ToolCall
from ..infra.clock import Clock, SystemClock
from ..infra.database import Database
from ..infra.redis import RedisSim, approval_key, decode, encode
from ..observability.audit import AuditLog, EventType

#: 等待人工处理的审批单状态（§52 状态机起点）。
WAITING = "WAITING_HUMAN"

#: 允许迁移的目标状态。
TERMINAL_STATUSES = ("APPROVED", "REJECTED", "MODIFIED")


class HumanApprovalManager:
    """§51-§53 人工审批管理器。

    :param redis: 协调层 —— 存审批单本体（短生命周期）
    :param db: 事实来源 —— 存 ``human.*`` 审计事件（长期留存）
    :param config: 平台配置；用于取审批单的 TTL（缺省则不过期）
    :param clock: 可注入时钟，让「什么时候批的」在推演里可控
    """

    def __init__(
        self,
        redis: RedisSim,
        db: Database,
        config: Optional[AppConfig] = None,
        *,
        clock: Optional[Clock] = None,
    ) -> None:
        self.redis = redis
        self.db = db
        self.config = config
        self.clock = clock or SystemClock()
        self.audit = AuditLog(db)

    # ==================================================================
    # 申请
    # ==================================================================
    def request(
        self,
        call: ToolCall,
        *,
        risk_level: RiskLevel,
        reason: str = "",
    ) -> ApprovalRecord:
        """为一次高风险调用开审批单，进入 ``WAITING_HUMAN``（§51/§67）。

        ``risk_level`` 是**必填**参数而不是从 metadata 里读：调用点（Risk Engine）
        才是做出「这次调用属于高风险」判断的地方，审批单必须忠实记录它的结论，
        而不是让存储层反推。
        """
        record = ApprovalRecord(
            call_id=call.call_id,
            idempotency_key=call.idempotency_key or call.ensure_idempotency_key(),
            tool_name=call.tool_name,
            risk_level=risk_level,
            arguments=dict(call.arguments),
            # 身份信息必须落单：批准之后要靠它把 ToolCall **原样重建**出来。
            # 少记一个 agent_id，批准后的执行就会用一个默认身份去跑 ——
            # 要么被权限层拦下（批了却跑不起来），要么绕过原本的身份约束。
            agent_id=call.agent_id,
            session_id=call.session_id,
            run_id=call.graph_run_id,
            tenant_id=call.tenant_id,
            logical_step_id=call.logical_step_id,
            status=WAITING,
            reason=reason,
            created_at=self.clock.now(),
        )
        self._save(record)
        self.audit.record(
            call.call_id,
            EventType.HUMAN_REQUESTED,
            tool_name=call.tool_name,
            risk_level=risk_level.value,
            reason=reason,
            idempotency_key=record.idempotency_key,
        )
        return record

    # ==================================================================
    # 查询
    # ==================================================================
    def get(self, call_id: str) -> Optional[ApprovalRecord]:
        """读审批单；不存在返回 ``None``（**只读操作不抛错**，方便轮询）。"""
        raw = self.redis.get(approval_key(call_id))
        payload = decode(raw)
        if payload is None:
            return None
        return ApprovalRecord.model_validate(payload)

    def pending(self) -> list[ApprovalRecord]:
        """全部待办审批单（按创建时间排序）—— 人工工作台的列表数据源。

        用 ``approval:*`` 扫描而不是维护一个索引集合：审批单数量在人类尺度上
        永远很小（个位数到几十），而多维护一个索引就多一处可能不同步的地方。
        """
        records: list[ApprovalRecord] = []
        for key in self.redis.keys("approval:*"):
            payload = decode(self.redis.get(key))
            if payload is None:
                continue
            record = ApprovalRecord.model_validate(payload)
            if record.status == WAITING:
                records.append(record)
        records.sort(key=lambda r: (r.created_at, r.call_id))
        return records

    def is_waiting(self, call_id: str) -> bool:
        """这个调用是否仍卡在人工审批上（Agent 恢复时用它决定「等还是重提交」）。"""
        record = self.get(call_id)
        return record is not None and record.status == WAITING

    # ==================================================================
    # 裁决
    # ==================================================================
    def approve(self, call_id: str, *, reviewer: str, reason: str = "") -> ApprovalRecord:
        """批准执行 —— ``WAITING_HUMAN -> APPROVED``（§52 的 APPROVE 分支）。"""
        record = self._require_waiting(call_id, action="approve")
        record.status = "APPROVED"
        record.reviewer = reviewer
        record.reason = reason
        record.decided_at = self.clock.now()
        self._save(record)
        self.audit.record(
            call_id,
            EventType.HUMAN_APPROVED,
            reviewer=reviewer,
            reason=reason,
            tool_name=record.tool_name,
        )
        return record

    def reject(self, call_id: str, *, reviewer: str, reason: str = "") -> ApprovalRecord:
        """拒绝执行 —— ``WAITING_HUMAN -> REJECTED``，上游据此走 ``CANCELLED``。

        拒绝是**终态**：被拒的调用不允许自动重试，否则人工判断就白做了。
        """
        record = self._require_waiting(call_id, action="reject")
        record.status = "REJECTED"
        record.reviewer = reviewer
        record.reason = reason
        record.decided_at = self.clock.now()
        self._save(record)
        self.audit.record(
            call_id,
            EventType.HUMAN_REJECTED,
            reviewer=reviewer,
            reason=reason,
            tool_name=record.tool_name,
        )
        return record

    def modify(
        self,
        call_id: str,
        *,
        reviewer: str,
        arguments: dict,
        reason: str = "",
    ) -> ApprovalRecord:
        """修改参数后放行 —— ``WAITING_HUMAN -> MODIFIED``（§52 MODIFY 分支）。

        **返回的 ``modified_arguments`` 是「候选参数」，不是「已校验参数」。**
        调用方拿到它之后必须重新走一遍完整校验链::

            Schema 校验 -> 确定性业务约束（§25）-> 注入检测（§24）-> 权限（§26）

        ……通过之后才允许执行；校验失败则应当回到参数自愈流程（§22-§23）
        或者直接拒绝，**绝不能**因为「这是人工改的」就跳过校验。
        理由见模块 docstring：跳过校验等于用一次审批动作绕掉整条防线。
        """
        record = self._require_waiting(call_id, action="modify")
        record.status = "MODIFIED"
        record.reviewer = reviewer
        record.reason = reason
        record.modified_arguments = dict(arguments)
        record.decided_at = self.clock.now()
        self._save(record)
        self.audit.record(
            call_id,
            EventType.HUMAN_MODIFIED,
            reviewer=reviewer,
            reason=reason,
            tool_name=record.tool_name,
            modified_arguments=dict(arguments),
            requires_revalidation=True,
        )
        return record

    def effective_arguments(self, call_id: str) -> Optional[dict]:
        """放行后真正该用的参数：``MODIFIED`` 用改后的，``APPROVED`` 用原参数。

        对尚未裁决的单子返回 ``None`` —— 调用点必须先问 :meth:`is_waiting`，
        而不是拿一个「看起来能用」的参数去执行。
        """
        record = self.get(call_id)
        if record is None:
            return None
        if record.status == "MODIFIED":
            return dict(record.modified_arguments or {})
        if record.status == "APPROVED":
            return dict(record.arguments)
        return None

    # ==================================================================
    # 内部
    # ==================================================================
    def _require_waiting(self, call_id: str, *, action: str) -> ApprovalRecord:
        """取一张**仍在等待**的审批单，取不到就按语义抛不同的错。

        * 不存在 -> :class:`HumanRejected`：没有放行许可，业务上等价于被拒。
        * 状态已迁移 -> :class:`ValueError`：调用方的时序错误。
          这两种错必须分开 —— 前者要给 Agent 一个「别再试了」的终态，
          后者应该炸在开发和联调阶段（见模块 docstring）。
        """
        record = self.get(call_id)
        if record is None:
            raise HumanRejected(
                f"审批单不存在: {call_id}，无法 {action}（没有拿到放行许可）",
                detail={"call_id": call_id, "action": action},
            )
        if record.status != WAITING:
            raise ValueError(
                f"非法状态迁移: {record.status} -> {action}（call_id={call_id}）。"
                f"只有 {WAITING} 状态的审批单可以裁决，已裁决的不能重复裁决"
            )
        return record

    def _save(self, record: ApprovalRecord) -> None:
        """写回 Redis。TTL 取幂等键的存活时间，理由见下。

        **审批单不该比幂等记录活得更久**：幂等记录一过期，这次调用的
        「执行权」就没了；此时再批准一张老审批单，等于放行一次**无法保证只执行一次**的
        副作用操作。所以两者用同一个 TTL 对齐生命周期。
        """
        ttl = self.config.idempotency_ttl_seconds if self.config is not None else None
        self.redis.set(approval_key(record.call_id), encode(record), ex=ttl)
