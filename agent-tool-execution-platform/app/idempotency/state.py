"""幂等状态读写与**原子迁移** —— ``idempotency:{key}`` 的唯一入口（§8 / §10）。

职责
----
把 §8 的幂等状态机（``PROCESSING -> SUCCESS / FAILED``）落成一组可被并发调用的方法：

============================ ====================================================
方法                          对应说明书
============================ ====================================================
:meth:`try_claim`             §10 ``SET key value NX EX`` —— 抢执行权
:meth:`mark_success`          §8 迁移到 SUCCESS（CAS，只有 PROCESSING 能迁）
:meth:`mark_failed`           §8 迁移到 FAILED（CAS）
:meth:`release_claim`         §10 提交失败时回滚执行权（仍是 CAS 删除）
:meth:`extend`                §14 长任务续期幂等键
============================ ====================================================

三个必须讲清楚的「为什么」
--------------------------
1. **幂等键的 TTL 必须长于 Agent 崩溃恢复的最坏耗时**（``AppConfig.idempotency_ttl_seconds``）。
   假设 TTL = 10 分钟，而 Agent 崩溃后重建上下文 + 拉 checkpoint + 重新提交花了 15 分钟：
   恢复时 ``GET idempotency:{key}`` 已经查不到键了 —— 平台会**误判为「从未提交过」**，
   于是重新执行一遍。对于 AT_LEAST_ONCE 的副作用 Tool，这就是一次真实的重复副作用。
   查不到键有两种含义（「没做过」与「做过但记录过期了」），TTL 就是用来把
   第二种含义压到「几乎不可能」的。默认 24h 不是拍脑袋，而是「远大于任何一次
   人工介入 / 排队 / 重启」。

2. **失败时必须回读现状，而不是直接报「重复」**。``SET NX`` 失败只说明「键存在」，
   调用方真正想知道的是「对方做到哪一步了」：还是 PROCESSING（该 WAIT）？
   已经 SUCCESS（直接取结果）？因此 :meth:`try_claim` 在失败路径上会重新 ``GET``
   并把现状一并返回。

3. **状态迁移必须 CAS**。反例：Worker A 执行成功写了 SUCCESS，但它的
   ``complete_success`` 因为网络重试晚到了 2 秒；此时一个慢 Worker（同键的旧尝试）
   回来了，如果直接覆盖，SUCCESS 就被改写成了 FAILED —— Agent 会拿着一个
   「失败」的结论去重试一件**已经成功的事**。所以脚本要求「当前仍是 PROCESSING」
   才允许迁移，否则拒绝并返回 0。

为什么是同步（sync）
--------------------
:class:`~app.infra.redis.RedisSim` 的每个命令都是同步的，单线程内同步方法之间不发生
协程切换，于是命令天然原子，与真实 Redis 单线程串行执行模型一致。所有关键迁移都收敛
在注册脚本里，**任何 await 都会破坏这个前提**。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..domain.enums import IdempotencyStatus, RecoveryAction
from ..domain.models import IdempotencyRecord, ProcessingClaim
from ..infra.clock import Clock
from ..infra.redis import RedisSim, encode, idempotency_key

logger = logging.getLogger(__name__)

TRANSITION_SCRIPT = "idempotency_transition"
"""CAS 状态迁移脚本（§8）：只有当前仍是 PROCESSING 才允许迁移。"""

RELEASE_SCRIPT = "idempotency_release"
"""CAS 归还执行权脚本（§10）：只有当前仍是 PROCESSING 才允许删除。"""


# ======================================================================
# 脚本（真实项目里是 Lua；此处是等价的同步 Python 实现，原子性一致）
# ======================================================================
def _script_transition(redis: RedisSim, keys: list[str], args: list[Any]) -> int:
    """``idempotency_transition``：读-校验-改-写 一次完成。

    :param keys: ``[idempotency:{key}]``
    :param args: ``[expected_status, patch_json, ttl_seconds]``

    把 patch 合并进**当前记录**（而不是整条覆盖）是刻意的：这样
    ``started_at`` / ``key_material`` 这类「谁提交的、原始参数是什么」的排障信息
    不会在迁移时丢失 —— 它们必须留在记录里，否则崩溃排查时无从下手。

    :return: ``1`` = 迁移成功；``0`` = 当前状态不是 ``expected_status``（拒绝覆盖）；
        ``-1`` = 键不存在（幂等键已过期，或从未被创建）
    """
    key = keys[0]
    expected_status = str(args[0])
    patch: dict = json.loads(args[1])
    ttl_seconds = int(args[2])

    raw = redis.get(key)
    if raw is None:
        return -1

    current = json.loads(raw)
    if current.get("status") != expected_status:
        # 已经 SUCCESS 了，就别让一个慢 Worker 把它改写成 FAILED。
        return 0

    current.update(patch)
    redis.set(key, json.dumps(current, ensure_ascii=False, default=str), ex=ttl_seconds)
    return 1


def _script_release(redis: RedisSim, keys: list[str], args: list[Any]) -> int:
    """``idempotency_release``：归还执行权 = CAS 删除。

    为什么不裸 ``DEL``：与分布式锁同理（§9）。极端但真实的序列是——
    A 抢到执行权 → A 卡住很久 → 幂等键 TTL 到期 → B 重新提交并抢到执行权 →
    A 恢复后走进「提交失败，回滚」分支 → 裸 ``DEL`` 删掉的是 **B 的执行权**，
    C 于是也能进来：B 与 C 同时执行同一个逻辑操作。
    加一层「当前仍是 PROCESSING」的校验，至少把「删掉别人刚写的新记录」
    这个窗口收敛到「两条记录都是 PROCESSING 且同键」（实际上不可能，同键只有一条记录）。

    :return: ``1`` = 已删除；``0`` = 状态不允许；``-1`` = 键不存在
    """
    key = keys[0]
    expected_status = str(args[0])
    raw = redis.get(key)
    if raw is None:
        return -1
    if json.loads(raw).get("status") != expected_status:
        return 0
    redis.delete(key)
    return 1


# ======================================================================
# 返回值对象
# ======================================================================
@dataclass
class ClaimResult:
    """``try_claim`` 的结果。

    :param acquired: 是否抢到执行权（``SET NX`` 是否成功）
    :param record: **无论成功失败都尽量带回记录**。失败路径上它是「对方的现状」，
        调用方据此区分 WAIT（对方 PROCESSING）/ 复用结果（对方 SUCCESS）/
        重试（键不存在）。只有键不存在时才是 ``None``。
    """

    acquired: bool
    record: IdempotencyRecord | None = None

    @property
    def status(self) -> IdempotencyStatus | None:
        return self.record.status if self.record else None


@dataclass
class WaitDecision:
    """``IdempotencyManager.decide`` 的裁决结果（§9~§13 的四分支汇聚点）。

    :param action: ``WAIT`` / ``RETRY`` / ``HUMAN`` / ``ABORT``
    :param reason: 面向人与审计的原因（会进执行事件），必须自带说明书章节号
    :param record: 裁决所依据的幂等记录（``None`` 表示键不存在）
    """

    action: RecoveryAction
    reason: str
    record: IdempotencyRecord | None = None


class IdempotencyStore:
    """``idempotency:{key}`` 的读写与原子迁移（§8 / §10）。

    本类**只碰 Redis**，不碰数据库：协调层与事实层的分工见 §43/§44 ——
    Redis 负责「现在谁在跑」，PostgreSQL 负责「到底发生了什么」。
    """

    def __init__(
        self,
        redis: RedisSim,
        *,
        clock: Clock | None = None,
        ttl_seconds: int = 86400,
    ) -> None:
        self._redis = redis
        self._clock: Clock = clock or redis.clock
        self._ttl_seconds = int(ttl_seconds)
        redis.register_script(TRANSITION_SCRIPT, _script_transition)
        redis.register_script(RELEASE_SCRIPT, _script_release)

    # ==================================================================
    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    @staticmethod
    def _key(key: str) -> str:
        return idempotency_key(key)

    def _now(self) -> datetime:
        """用注入时钟取时间，保证 ``ManualClock`` 下记录的 ``started_at`` 与租约一致。"""
        return self._clock.now()

    # ==================================================================
    # 读
    # ==================================================================
    def get(self, key: str) -> IdempotencyRecord | None:
        """读幂等记录；键不存在返回 ``None``。

        **刻意不吞解析异常**：脏数据如果被静默当成「键不存在」，调用方会把它
        理解为「从未提交过」并重新执行 —— 这正是幂等机制要防的事故。
        宁可让 pydantic 的 ValidationError 冒出来（fail loudly）。
        """
        raw = self._redis.get(self._key(key))
        if raw is None:
            return None
        return IdempotencyRecord.from_json(raw)

    def ttl(self, key: str) -> int:
        """剩余秒数：``-1`` 无过期，``-2`` 键不存在。"""
        return self._redis.ttl(self._key(key))

    # ==================================================================
    # 抢执行权（§10）
    # ==================================================================
    def try_claim(
        self,
        *,
        key: str,
        call_id: str,
        worker_id: str | None = None,
        lease_id: str | None = None,
        key_material: str | None = None,
    ) -> ClaimResult:
        """§10 的 ``SET key value NX EX`` —— 抢「这个逻辑操作」的执行权。

        :return: ``acquired=True`` 且 ``record`` 是刚写入的 PROCESSING 记录；
            失败时 ``acquired=False`` 且 ``record`` 是**回读到的现状**。

        为什么失败还要多花一次 ``GET``：``SET NX`` 的失败返回值里没有任何信息，
        而调用方的下一步动作完全取决于对方的进度 ——
        对方 PROCESSING 就该 WAIT（§11），对方 SUCCESS 就该直接复用结果（§12）。
        这一次回读不是浪费，是把「重复提交」变成「幂等命中」的关键。
        """
        record = IdempotencyRecord(
            status=IdempotencyStatus.PROCESSING,
            call_id=call_id,
            worker_id=worker_id,
            lease_id=lease_id,
            started_at=self._now(),
            key_material=key_material,
        )
        # 写入用 §10 的 ProcessingClaim（「执行权占有声明」），
        # 读回用 §8 的 IdempotencyRecord —— 后者的字段是前者的超集，同一份 JSON 两边都能解。
        claim = ProcessingClaim(
            call_id=call_id,
            worker_id=worker_id,
            lease_id=lease_id,
            started_at=record.started_at,
            key_material=key_material,
        )
        acquired = self._redis.set(
            self._key(key), encode(claim), nx=True, ex=self._ttl_seconds
        )
        if acquired:
            return ClaimResult(True, record)

        # 抢占失败 —— 回读现状，让调用方知道对方走到哪一步了。
        current = self.get(key)
        if current is None:
            # 极窄的竞态：SET NX 失败说明键存在，但紧接着它过期/被删了。
            # 如实返回 None，由调用方按「键不存在」分支处理（等价于可以重试）。
            logger.debug("idempotency claim lost and record vanished: %s", key)
        return ClaimResult(False, current)

    # ==================================================================
    # 状态迁移（§8，全部 CAS）
    # ==================================================================
    def mark_processing_owner(
        self, key: str, *, worker_id: str, lease_id: str
    ) -> bool:
        """把「谁在执行」补写到记录上（仍是 CAS：只有 PROCESSING 能改）。

        用途：抢执行权时还不知道 lease_id（**先抢幂等键、再抢租约**，或者反过来），
        拿到租约后回填，于是排障时能从幂等记录直接定位到当时那把租约。
        """
        return self._transition(
            key,
            IdempotencyStatus.PROCESSING,
            {"worker_id": worker_id, "lease_id": lease_id},
        )

    def mark_success(self, key: str, *, call_id: str, result_id: str | None) -> bool:
        """PROCESSING -> SUCCESS（CAS）。

        :return: ``True`` = 迁移成功；``False`` = 记录已不是 PROCESSING（通常是
            已经 SUCCESS，另一个慢 Worker 的迟到写入被正确拒绝）或键已不存在。
        """
        return self._transition(
            key,
            IdempotencyStatus.PROCESSING,
            {
                "status": IdempotencyStatus.SUCCESS.value,
                "call_id": call_id,
                "result_id": result_id,
                "finished_at": self._now().isoformat(),
                "error": None,
                "error_type": None,
            },
        )

    def mark_failed(
        self, key: str, *, call_id: str, error: str, error_type: str
    ) -> bool:
        """PROCESSING -> FAILED（CAS）。

        注意 FAILED **不等于**「不可再试」：去向由 :mod:`app.domain.policy`
        的 Recovery Policy 按 ``error_type`` 决定（§35），
        并且 attempt 超限时会在 :meth:`app.idempotency.manager.IdempotencyManager.decide`
        里被降级成 ABORT。
        """
        return self._transition(
            key,
            IdempotencyStatus.PROCESSING,
            {
                "status": IdempotencyStatus.FAILED.value,
                "call_id": call_id,
                "error": error,
                "error_type": error_type,
                "finished_at": self._now().isoformat(),
            },
        )

    def _transition(
        self, key: str, expected: IdempotencyStatus, patch: dict[str, Any]
    ) -> bool:
        """统一走注册脚本做 CAS，并把返回码翻译成人话。"""
        code = int(
            self._redis.eval_script(
                TRANSITION_SCRIPT,
                [self._key(key)],
                [
                    expected.value,
                    json.dumps(patch, ensure_ascii=False, default=str),
                    self._ttl_seconds,
                ],
            )
        )
        if code == 1:
            return True
        if code == -1:
            logger.warning("idempotency record missing on transition: %s", key)
        else:
            # 这是**正常且必要**的拒绝：说明有人想覆盖一个已经终态的记录。
            logger.info("idempotency transition rejected (not %s): %s", expected.value, key)
        return False

    # ==================================================================
    # 归还 / 续期 / 清理
    # ==================================================================
    def ensure_owned(
        self,
        *,
        key: str,
        call_id: str,
        worker_id: str,
        lease_id: str | None = None,
        key_material: str | None = None,
    ) -> ClaimResult:
        """确保「这次执行」持有幂等执行权 —— Worker 真正需要的那个操作。

        为什么不直接调 :meth:`mark_processing_owner`：它是个**纯粹的 CAS 回填**，
        记录不存在或状态不对就返回 ``False``，还会打一条 WARNING。
        但「记录不存在」在**回收路径**上是完全正常的 ——

        ::

            Worker A 死 -> Reaper 判定可接管 -> release_claim（删掉记录）
                                            -> 任务重新入队
            Worker B 取到任务，此时幂等键已经不在

        如果 B 只是回填失败就放弃，任务会被 ACK 丢掉；如果 B 硬着头皮执行，
        执行完又无处 `complete_success`，幂等键彻底消失 —— 下次同样的调用
        会被当成新请求再跑一遍。

        所以这里把三种情形一次处理干净，语义是「**我要成为这个键的合法持有者**」：

        ==================== ============================================================
        现状                  动作
        ==================== ============================================================
        不存在                认领（§10 ``SET NX``）—— 回收路径的正常形态
        PROCESSING            回填 ``worker_id`` / ``lease_id`` —— 常态（Gateway 先认领过）
        FAILED                重新认领 —— 平台已判定要重试，钥匙交还给下一个执行者
        SUCCESS               **拒绝** —— 已经做完了，绝不能重跑（§12）
        ==================== ============================================================

        返回的 :class:`ClaimResult` 与 :meth:`try_claim` 一致：
        ``acquired=False`` 时 ``record`` 是回读到的现状，调用方据此判断是
        「别人在跑（让路）」还是「已经成功（直接复用）」。
        """
        current = self.get(key)

        if current is None or current.status == IdempotencyStatus.FAILED:
            # 已经 FAILED 的记录要先让位，否则 SET NX 会因为键还存在而失败。
            # 这个删除是安全的：FAILED 说明平台已经决定「这一轮不算数」。
            if current is not None:
                self.delete(key)
            return self.try_claim(
                key=key,
                call_id=call_id,
                worker_id=worker_id,
                lease_id=lease_id,
                key_material=key_material,
            )

        if current.status == IdempotencyStatus.PROCESSING:
            self.mark_processing_owner(key, worker_id=worker_id, lease_id=lease_id or "")
            refreshed = self.get(key)
            return ClaimResult(acquired=True, record=refreshed or current)

        # SUCCESS —— 「已经真的做完了」，重复投递到此为止
        return ClaimResult(acquired=False, record=current)

    def release_claim(self, key: str) -> bool:
        """执行权归还 —— 提交失败（或提交前被拦）时把占位让出来（§10）。

        典型调用点：Gateway 抢到执行权之后，才发现 Tool 未注册 / 权限不足，
        属于「还没真正开始执行」的失败。此时如果留着 PROCESSING 记录，
        幂等键在 TTL 内会一直把后续提交挡成 WAIT ——
        而这本来是一个可以立刻修正参数重试的场景。
        """
        code = int(
            self._redis.eval_script(
                RELEASE_SCRIPT,
                [self._key(key)],
                [IdempotencyStatus.PROCESSING.value],
            )
        )
        return code == 1

    def extend(self, key: str, seconds: int) -> bool:
        """给幂等记录续期（§14 长任务的配套动作）。

        长任务（``run_test`` 这类分钟级）在租约不断续期的同时，幂等键也应当跟着续，
        否则会出现「执行还在跑，但幂等键先过期」的错位状态。
        """
        return bool(self._redis.expire(self._key(key), int(seconds)))

    def delete(self, key: str) -> bool:
        """删除幂等记录（仅供管理面 / 人工介入后重新放行使用）。

        **不要**在正常失败路径上调用它 —— 那等于宣布「这件事从没发生过」，
        后续任何一次提交都会被当成首次执行。
        """
        return self._redis.delete(self._key(key)) > 0


__all__ = [
    "ClaimResult",
    "IdempotencyStore",
    "RELEASE_SCRIPT",
    "TRANSITION_SCRIPT",
    "WaitDecision",
]
