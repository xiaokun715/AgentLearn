"""租约回收器 —— Worker 崩溃之后，谁来接手（§56）。

这是整套可靠性设计里**最危险的一段代码**：它会在「一个 Tool 可能正在执行」的时候，
决定要不要让**另一个** Worker 再执行一次。所以它的每一步都必须有依据。

§56 最重要的一段话
------------------
    Worker A 的租约过期了，**并不等于 Worker A 死了** ——
    它可能只是网络断开，人还活着，Tool 还在跑，副作用（转账、发消息、删表）已经发出去了。

于是问题变成：**能不能让 Worker B 直接接管重跑？** 答案取决于 Tool 的副作用是否可重复，
也就是 :class:`~app.domain.enums.IdempotencyLevel`（§57）存在的全部理由::

    PURE / IDEMPOTENT   -> 重跑一次不改变任何结果（calculator / run_test）   -> RETRY
    AT_LEAST_ONCE       -> 重跑可能多一次投递（message publish）            -> 先查状态，查不到 -> HUMAN
    NON_IDEMPOTENT      -> 重跑就是第二次副作用（payment / delete）         -> HUMAN

注意这里的取舍：**宁可让人来做一次判断，也不要机器自动产生第二次副作用。**
一次人工确认的成本是几十秒；一次重复扣款的成本是不可逆的。

单次回收的完整流程（§56 流程图的落点）::

    ① expired() 拿到过期租约候选（zset 区间扫描 + 双重核对）
    ② 读 DB：执行记录必须仍是 PROCESSING（已终态的一律跳过 —— 这是最重要的一道过滤）
    ③ Acquire Recovery Lock：抢 ``recovery:{call_id}``
       —— 防止多个 Reaper 同时接管同一个 call（重复恢复 = 变相的重复执行）
    ④ 从 Registry 取 Tool 的 idempotency_level / risk_level（§57 / §51）
    ⑤ 分叉：
         crash_safe_to_retry 且 risk != HIGH
             -> RETRY：force_release 旧租约 + 归还幂等执行权
                        + on_recover(call_id) 重新入队 + 写事件 lease.reaped.retry
         否则
             -> HUMAN：写事件 lease.reaped.human + 执行状态置 WAITING_HUMAN
    ⑥ 释放 Recovery Lock（contextmanager 的 finally）

为什么是同步（sync）
--------------------
Reaper 的每一次判断都是「读 DB -> 抢锁 -> 写 DB -> 写 Redis」的同步序列。
:class:`~app.infra.redis.RedisSim` 的每个命令都是同步的，单线程内同步方法之间不发生
协程切换，于是命令天然原子，与真实 Redis 单线程串行执行模型一致；
跨进程的互斥由第 ③ 步的分布式锁保证。**任何 await 都会破坏这个前提**。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from ..config import AppConfig
from ..domain.enums import (
    ExecutionStatus,
    IdempotencyLevel,
    RecoveryAction,
    RiskLevel,
)
from ..domain.models import ExecutionRecord, ToolMetadata
from ..infra.clock import Clock
from ..infra.database import Database
from ..infra.redis import RedisSim
from ..idempotency.lock import DistributedLock
from .manager import LeaseManager

logger = logging.getLogger(__name__)

RECOVERY_LOCK_PREFIX = "recovery:"
"""恢复锁的键前缀：多个 Reaper 之间「同一个 call 只接管一次」的互斥点（§56 流程图）。"""

EVENT_RETRY = "lease.reaped.retry"
EVENT_HUMAN = "lease.reaped.human"

UNKNOWN_TOOL_METADATA = ToolMetadata(
    name="<unknown>",
    idempotency_level=IdempotencyLevel.AT_LEAST_ONCE,
    risk_level=RiskLevel.MEDIUM,
)
"""**查不到 Tool 元数据时的兜底：一律不自动接管。**

为什么不兜成 PURE（最省事、最容易让演示跑通）：那等于「因为我们不知道它做了什么，
所以假设它随便重跑都没事」——把最不确定的情况当成最安全的情况处理。
正确的方向相反：不知道 -> 当作不可安全重跑 -> 交给人工（§56/§57）。
``AT_LEAST_ONCE`` 的 ``crash_safe_to_retry`` 为 ``False``，因此必然落到 HUMAN 分支。
"""


@dataclass
class ReapOutcome:
    """一次回收的结论 —— 可直接进审计事件 / 观测指标。

    :param recovered: 是否**已实际重新入队**。只有 RETRY 分支才可能为 ``True``，
        且必须由 ``on_recover`` 回调真的返回 ``True`` 才算数 ——
        没有回调时它保持 ``False``，提醒调用方「DB 里标了 QUEUED，但还没人真去排队」。
    """

    call_id: str
    worker_id: str | None
    action: RecoveryAction
    idempotency_level: IdempotencyLevel
    risk_level: RiskLevel
    reason: str
    recovered: bool

    def to_payload(self) -> dict[str, Any]:
        """转成可写进 ``execution_event.payload`` 的 dict。"""
        return {
            "call_id": self.call_id,
            "worker_id": self.worker_id,
            "action": self.action.value,
            "idempotency_level": self.idempotency_level.value,
            "risk_level": self.risk_level.value,
            "reason": self.reason,
            "recovered": self.recovered,
        }


def _lookup_metadata(registry: Any, tool_name: str) -> tuple[ToolMetadata, bool]:
    """从 Registry 取 Tool 元数据。

    刻意用**鸭子类型**而不是硬绑 :class:`~app.tools.registry.ToolRegistry`：
    只要能回答「这个 Tool 的幂等性等级与风险等级是什么」就够了 ——
    可以是 ``ToolRegistry``（``.metadata()``），也可以是 ``{name: ToolMetadata}`` 字典
    （测试 / 恢复流程里常用）。耦合到具体实现会让这段最需要被复用的逻辑最难复用。

    注意 ``Mapping`` 必须**第一个**判断：``dict`` 也有 ``get`` 方法，
    若先走 ``hasattr(registry, "get")`` 分支，``{"run_test": ToolMetadata(...)}``
    会被误当成 ToolRegistry 解析而静默退化成兜底元数据 ——
    表现为「明明注册了却总是转人工」，是极难排查的一类错误。

    :return: ``(metadata, found)``；``found=False`` 时返回的是
        :data:`UNKNOWN_TOOL_METADATA` 的副本（名字替换成实际 Tool 名）
    """
    try:
        if isinstance(registry, Mapping):
            found = registry.get(tool_name)
            if isinstance(found, ToolMetadata):
                return found, True
            return UNKNOWN_TOOL_METADATA.model_copy(update={"name": tool_name}), False
        if hasattr(registry, "metadata"):
            return registry.metadata(tool_name), True
        if hasattr(registry, "get"):
            spec = registry.get(tool_name)
            metadata = getattr(spec, "metadata", None)
            if isinstance(metadata, ToolMetadata):
                return metadata, True
    except Exception as exc:  # ToolNotFound / 未注册 / 结构不符，统统走兜底
        logger.debug("tool metadata lookup failed for %s: %s", tool_name, exc)
    return UNKNOWN_TOOL_METADATA.model_copy(update={"name": tool_name}), False


class LeaseReaper:
    """过期租约的扫描与接管（§56）。

    :param redis: 协调层（清理旧租约、归还幂等执行权）
    :param db: 事实层（读执行记录、写回收事件）
    :param config: 平台配置（``lock_ttl_seconds`` / ``lease_ttl_seconds``）
    :param registry: Tool 元数据来源 —— ``ToolRegistry`` 或 ``{name: ToolMetadata}``
    :param on_recover: 重新入队的回调（返回是否真的入队成功）。
        平台里通常是 ``scheduler.enqueue(call_id)``；不注入时只会把 DB 状态置回 QUEUED，
        真正的排队动作留给调用方 —— 这样 Reaper 就不必依赖调度器的实现。
    """

    def __init__(
        self,
        redis: RedisSim,
        db: Database,
        config: AppConfig,
        registry: Any,
        *,
        clock: Clock | None = None,
        lease_manager: LeaseManager | None = None,
        idempotency: Any | None = None,
        on_recover: Callable[[str], bool] | None = None,
    ) -> None:
        self._redis = redis
        self._db = db
        self._config = config
        self._registry = registry
        self._clock: Clock = clock or redis.clock
        self._leases = lease_manager or LeaseManager(redis, config, clock=self._clock)
        self._idempotency = idempotency
        self._on_recover = on_recover

        # 身份标识：只用于日志/锁 value 的人类可读归属（一把锁只有一个赢家，不需要全局唯一 ID 服务）
        self._owner = f"reaper-{id(self):x}"
        self._recovery_lock = DistributedLock(
            redis,
            ttl_seconds=config.lock_ttl_seconds,
            clock=self._clock,
        )

    # ==================================================================
    @property
    def lease_manager(self) -> LeaseManager:
        return self._leases

    def run_once(self) -> list[ReapOutcome]:
        """扫一轮并执行回收 —— :meth:`scan_once` 的别名，语义更明确。

        「跑一轮」而不是「常驻循环」是刻意的：常驻循环属于调度器的职责，
        Reaper 只提供**可被反复调用的幂等单轮**，这样它既能被线程定时驱动，
        也能在测试里被直接调用（``ManualClock.advance(60); reaper.run_once()``）。
        """
        return self.scan_once()

    def scan_once(self) -> list[ReapOutcome]:
        """扫一遍过期租约，返回**本轮真正做出了裁决**的那些 call。

        没有产生 :class:`ReapOutcome` 的情况都只是「跳过」，不是失败：
        执行已终态、锁被别人抢走、租约刚刚被续上 —— 它们都会在下一轮被重新评估。
        """
        outcomes: list[ReapOutcome] = []
        for call_id in self._leases.expired():
            outcome = self._reap_one(call_id)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    # ==================================================================
    # 单个 call 的回收
    # ==================================================================
    def _reap_one(self, call_id: str) -> ReapOutcome | None:
        """按 §56 流程图处理一个过期租约。"""
        record = self._db.get_execution(call_id)
        if record is None:
            # 租约在 DB 里没有对应记录：通常是历史残留（数据被清理过）。
            # 不写事件、只清索引，避免产生无法追责的审计噪声。
            logger.info("lease without execution record, releasing: %s", call_id)
            self._leases.force_release(call_id)
            return None

        if record.status != ExecutionStatus.PROCESSING:
            # **最重要的一道过滤**：执行已经走到终态（SUCCESS / FAILED / CANCELLED /
            # CANCELLING 等），过期租约只是收尾没做干净。
            # 少了这一条，Reaper 会把一个已经成功的 call 重新丢回队列 ——
            # 那是一次彻头彻尾的重复执行，且完全绕过了幂等键（幂等键此刻是 SUCCESS）。
            self._leases.force_release(call_id)
            return None

        # ③ Acquire Recovery Lock（§56）：多个 Reaper 同时看到一个过期租约是常态，
        # 必须有一个地方裁决「谁来接管」。抢不到就直接放弃本轮 —— 不是错误，
        # 只是「已经有别的 Reaper 在做了」。
        with self._recovery_lock.hold(
            f"{RECOVERY_LOCK_PREFIX}{call_id}", owner=self._owner
        ) as acquired:
            if not acquired:
                logger.debug("recovery lock held by another reaper: %s", call_id)
                return None
            return self._decide_and_apply(call_id, record)

    def _decide_and_apply(
        self, call_id: str, record: ExecutionRecord
    ) -> ReapOutcome | None:
        """锁内做判断与落地（RETRY / HUMAN 两条路）。"""
        # 竞态防护（双重检查）：从 expired() 到拿到锁之间过了一小段时间，
        # 期间原 Worker 可能刚好续上了租约（网络抖了一下又通了）。
        # 若它已经活着，本轮的「过期」判断就作废了 —— 绝不能抢一个活人的执行权。
        if self._leases.is_alive(call_id):
            logger.info("lease revived before reap, skipping: %s", call_id)
            return None

        lease = self._leases.get(call_id)
        worker_id = lease.worker_id if lease else record.worker_id
        lease_id = lease.lease_id if lease else record.lease_id
        if worker_id is None:
            # 租约键可能已经被 Redis 的 TTL 清掉（`expired()` 的第二种情形），
            # 此时从幂等记录里补出「上一任执行者」—— 审计事件必须能回答
            # 「这次回收是在收拾谁的摊子」，否则事后无法追责与复盘。
            worker_id = self._worker_from_idempotency(record.idempotency_key)

        metadata, found = _lookup_metadata(self._registry, record.tool_name)
        level = metadata.idempotency_level
        risk = metadata.risk_level

        if level.crash_safe_to_retry and risk != RiskLevel.HIGH:
            return self._apply_retry(
                call_id, record, worker_id=worker_id, lease_id=lease_id,
                level=level, risk=risk,
            )
        return self._apply_human(
            call_id, record, worker_id=worker_id, lease_id=lease_id,
            level=level, risk=risk, found=found,
        )

    # ------------------------------------------------------------------
    def _apply_retry(
        self,
        call_id: str,
        record: ExecutionRecord,
        *,
        worker_id: str | None,
        lease_id: str | None,
        level: IdempotencyLevel,
        risk: RiskLevel,
    ) -> ReapOutcome:
        """§56 的自动接管分支：重跑不会改变结果（PURE / IDEMPOTENT 且非高风险）。"""
        # 1) 清掉旧租约与它的索引。此刻旧 Worker 即使「复活」也续不上租约（§15 CAS），
        #    它会在下一次 renew / 写结果前被 LeaseLost 拦下。
        self._leases.force_release(call_id)

        # 2) 归还幂等执行权。不做这一步，重新入队的那个 Worker 会 SET NX 失败、
        #    回读到一条陈旧的 PROCESSING 记录，于是永远停在 WAIT —— 恢复流程空转。
        #    这里的语义是「我们已经判定前一次执行作废，把这把钥匙交还给下一个执行者」。
        released_claim = self._release_idempotency_claim(record.idempotency_key)

        # 3) 回队列（DB 状态先回落，再交给调度器排队；顺序反了会出现
        #    「队列里有了，但 DB 还写着 PROCESSING」的短暂错位，查状态时容易误判）
        self._db.update_execution(
            call_id,
            status=ExecutionStatus.QUEUED,
            worker_id=None,
            lease_id=None,
            attempt=record.attempt + 1,  # 接管也是一次尝试，必须计数（§36 上限闸门靠它）
        )
        recovered = bool(self._on_recover(call_id)) if self._on_recover else False

        reason = (
            f"§56/§57 租约过期且执行状态不确定；Tool 的幂等性等级为 {level.value}"
            f"（crash_safe_to_retry=True），风险为 {risk.value}（非 HIGH）："
            "允许新 Worker 接管重跑，重跑不会改变最终结果"
        )
        payload = {
            "worker_id": worker_id,
            "lease_id": lease_id,
            "previous_attempt": record.attempt,
            "idempotency_level": level.value,
            "risk_level": risk.value,
            "idempotency_claim_released": released_claim,
            "requeued": recovered,
            "reason": reason,
        }
        self._db.append_event(call_id, EVENT_RETRY, payload)
        logger.warning("lease reaped -> RETRY: call=%s worker=%s", call_id, worker_id)
        return ReapOutcome(
            call_id=call_id,
            worker_id=worker_id,
            action=RecoveryAction.RETRY,
            idempotency_level=level,
            risk_level=risk,
            reason=reason,
            recovered=recovered,
        )

    def _apply_human(
        self,
        call_id: str,
        record: ExecutionRecord,
        *,
        worker_id: str | None,
        lease_id: str | None,
        level: IdempotencyLevel,
        risk: RiskLevel,
        found: bool,
    ) -> ReapOutcome:
        """§56 的转人工分支：重跑可能产生第二次真实副作用。

        这里**不**调用 ``on_recover``，也**不**动幂等键 ——
        保持 PROCESSING 原样，是对「Worker A 也许还活着」这件事最诚实的表达：
        结果不确定，谁都不许继续写，等人工/对账来收尾。
        """
        # 仅清理租约索引与键（它已经过期，留着只会让 Reaper 每轮重复扫到同一个 call）
        self._leases.force_release(call_id)

        self._db.update_execution(
            call_id,
            status=ExecutionStatus.WAITING_HUMAN,
            worker_id=None,
            lease_id=None,
        )
        reason = (
            f"§56/§57 租约过期 + 执行状态不确定，且 Tool 的幂等性等级为 {level.value}"
            f"（crash_safe_to_retry=False）、风险为 {risk.value}："
            "Worker 可能只是网络断开而非死亡，自动重跑会产生第二次副作用 —— "
            "转人工确认（RECOVERY_REQUIRED / WAITING_HUMAN）"
        )
        if not found:
            reason = (
                f"§56 Tool 元数据缺失（registry 中查不到 {record.tool_name}）："
                "无法证明重跑安全，按最保守路径转人工"
            )

        payload = {
            "worker_id": worker_id,
            "lease_id": lease_id,
            "idempotency_level": level.value,
            "risk_level": risk.value,
            "tool_metadata_found": found,
            "reason": reason,
        }
        self._db.append_event(call_id, EVENT_HUMAN, payload)
        logger.warning("lease reaped -> HUMAN: call=%s worker=%s", call_id, worker_id)
        return ReapOutcome(
            call_id=call_id,
            worker_id=worker_id,
            action=RecoveryAction.HUMAN,
            idempotency_level=level,
            risk_level=risk,
            reason=reason,
            recovered=False,
        )

    # ------------------------------------------------------------------
    def _idempotency_store(self) -> Any | None:
        """把 ``idempotency`` 参数归一化成 store（接受 manager 或 store 本身）。"""
        if self._idempotency is None:
            return None
        return getattr(self._idempotency, "store", self._idempotency)

    def _worker_from_idempotency(self, key: str) -> str | None:
        """从幂等记录里补出上一任执行者（审计用）。"""
        if not key:
            return None
        store = self._idempotency_store()
        check = getattr(store, "get", None)
        if check is None:
            return None
        record = check(key)
        return record.worker_id if record else None

    def _release_idempotency_claim(self, key: str) -> bool:
        """归还幂等执行权（§10 ``release_claim``）。

        ``idempotency`` 参数接受 ``IdempotencyManager``（取它的 ``.store``）
        或直接一个 ``IdempotencyStore`` —— 两种注入方式都很自然，不必二选一。
        未注入时返回 ``False``：Reaper 仍然完成回收，只是留下一条 PROCESSING 的幂等记录，
        由调用方自己负责后续（这也是为什么返回值要写进事件 payload）。
        """
        if not key:
            return False
        store = self._idempotency_store()
        release = getattr(store, "release_claim", None)
        if release is None:
            logger.warning("idempotency backend has no release_claim(): %r", type(store))
            return False
        return bool(release(key))


__all__ = ["LeaseReaper", "ReapOutcome", "UNKNOWN_TOOL_METADATA"]
