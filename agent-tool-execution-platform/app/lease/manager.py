"""租约 —— 「谁有权继续写结果」的唯一凭据（§14 / §15）。

职责
----
:class:`LeaseManager` 维护 ``lease:{call_id}`` 这把**带 TTL 的所有权凭证**：

============ ==========================================================
方法          语义
============ ==========================================================
``acquire``  §14 ``SET lease:{call_id} NX EX ttl`` —— 没有租约才能拿到
``renew``    §15 compare-and-renew：只有当前 ``lease_id`` 持有者能续
``release``  §15 compare-and-release：只有当前 ``lease_id`` 持有者能还
``is_alive`` 租约存在且 ``expire_at > now``
``expired``  §56 给 Reaper 的过期候选（zset 区间扫描 + 双重核对）
============ ==========================================================

为什么必须 compare-and-renew（§15 的重点）
------------------------------------------
这不是「防御性编程」，而是一个真实会发生的事故序列::

    t0   Worker A 拿到 lease（TTL=30s），开始跑一个 5 分钟的任务
    t10  A 所在机房网络抖动，A 的心跳发不出去
    t30  租约到期（A 其实还活着，只是网断了）
    t31  Reaper 判定 A 已死 -> 让 Worker B 接管（§56）
    t40  网络恢复，A 的心跳「复活」并调用 renew —— 如果 renew 不校验 lease_id，
         它就**续期成功了**：此时 A 与 B 都认为自己是合法执行者，
         两个 Worker 都会去写 tool_result，最后落库的是后到的那个（可能更旧）

    正确行为：A 的 renew 读到 value 里的 lease_id 已经是 B 的（或是 B 新建的另一把），
    与自己的不一致 -> 返回 0 -> A 必须立刻停止执行并抛
    :class:`~app.domain.errors.LeaseLost`，绝不写结果。

同一条规则也适用于 ``release``：A 恢复后调 release 不能把 B 的租约删掉（§9 同款问题）。

为什么用 zset 做索引
--------------------
``keys("*")`` 扫描在生产环境是禁用操作（Redis 单线程，会阻塞整个实例）。
Reaper 需要「找出所有 ``expire_at <= now`` 的租约」，标准做法是把这个条件编码成
**有序集合的分数**，用 ``ZRANGEBYSCORE`` 做区间查询（复杂度 O(log N + M)）。
代价是索引与实际键可能短暂不一致（脚本写租约与写索引之间、或键被 TTL 自然淘汰），
所以 :meth:`expired` 必须做**双重核对**。

为什么是同步（sync）
--------------------
:class:`~app.infra.redis.RedisSim` 的每个命令都是同步的，单线程内同步方法之间不发生协程
切换，于是每个命令天然原子，与真实 Redis 单线程串行执行模型一致。租约的
「读-比较-续期」必须整体原子（否则 compare 与 renew 之间就有窗口），所以它被写进
注册脚本里。**任何 await 都会破坏这个前提**。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..config import AppConfig
from ..domain.models import LeaseRecord, new_lease_id
from ..infra.clock import Clock
from ..infra.redis import LEASE_ZSET, RedisSim, encode, lease_key

logger = logging.getLogger(__name__)

RENEW_SCRIPT = "lease_renew"
"""§15 compare-and-renew。"""

RELEASE_SCRIPT = "lease_release"
"""§15 compare-and-release。"""


# ======================================================================
# 脚本（真实项目里是 Lua；此处是等价的同步 Python 实现，原子性一致）
# ======================================================================
def _script_renew(redis: RedisSim, keys: list[str], args: list[Any]) -> int:
    """``lease_renew``：只有当前持有者能续租。

    :param keys: ``[lease:{call_id}, lease:expirations]``
    :param args: ``[call_id, lease_id, worker_id, ttl_seconds]``
    :return: ``1`` = 续租成功；``0`` = 租约不存在（已被 Reaper 回收）或
        ``lease_id`` / ``worker_id`` 与当前持有者不一致（**本 Worker 已经失去执行权**）
    """
    call_id = str(args[0])
    lease_id = str(args[1])
    worker_id = str(args[2])
    ttl_seconds = float(args[3])

    raw = redis.get(keys[0])
    if raw is None:
        # 租约已被 TTL 淘汰 —— 有没有被 B 接管都一样：本 Worker 已经没有执行权了。
        return 0

    record = json.loads(raw)
    # lease_id 是「哪一把租约」，worker_id 是「谁」；两者都比对，
    # 因为同一把 lease_id 被另一个 Worker 复用是不可能的，而同一个 Worker 重新
    # acquire 会拿到**新的** lease_id —— 两个字段一起比，语义最不容易误判。
    if record.get("lease_id") != lease_id or record.get("worker_id") != worker_id:
        return 0

    now = redis.clock.time()
    record["expire_at"] = now + ttl_seconds
    record["ttl_seconds"] = int(ttl_seconds)
    # ex=ttl 同时把 Redis 侧的 TTL 重新武装（不用 EXPIRE 是为了少一次往返）
    redis.set(keys[0], json.dumps(record, ensure_ascii=False, default=str), ex=ttl_seconds)
    # 索引同步更新：Reaper 靠这个分数判断「谁过期了」，它必须跟着续租一起往前推。
    redis.zadd(keys[1], {call_id: record["expire_at"]})
    return 1


def _script_release(redis: RedisSim, keys: list[str], args: list[Any]) -> int:
    """``lease_release``：只有当前持有者能归还租约。

    与 :mod:`app.idempotency.lock` 的 ``lock_release`` 是同一个套路（§9）：
    裸 ``DEL`` 会把「已经被别人接管的租约」删掉，等于给并发执行发通行证。
    """
    call_id = str(args[0])
    lease_id = str(args[1])

    raw = redis.get(keys[0])
    if raw is None:
        # 租约本来就没了，顺手把索引清干净（幂等：重复 release 不报错）
        redis.zrem(keys[1], call_id)
        return 0

    record = json.loads(raw)
    if record.get("lease_id") != lease_id:
        return 0

    redis.delete(keys[0])
    redis.zrem(keys[1], call_id)
    return 1


class LeaseManager:
    """租约的申请、续期、释放与过期扫描。

    :param redis: 协调层
    :param config: 平台配置（``lease_ttl_seconds``）
    :param clock: 时钟；缺省与 ``RedisSim`` 共用，避免「TTL 到期」与「expire_at 到期」
        用两套时间算而错位。
    """

    def __init__(
        self,
        redis: RedisSim,
        config: AppConfig,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._redis = redis
        self._config = config
        self._clock: Clock = clock or redis.clock
        self._ttl = int(config.lease_ttl_seconds)
        redis.register_script(RENEW_SCRIPT, _script_renew)
        redis.register_script(RELEASE_SCRIPT, _script_release)

    # ==================================================================
    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    @staticmethod
    def _key(call_id: str) -> str:
        return lease_key(call_id)

    # ==================================================================
    # 申请 / 续期 / 归还
    # ==================================================================
    def acquire(
        self, call_id: str, *, worker_id: str, ttl_seconds: int | None = None
    ) -> LeaseRecord | None:
        """申请租约。

        **只有当前没有租约时才能拿到**（``SET NX``）——
        这就是「同一个 call 同一时刻只有一个合法执行者」的全部保证。
        拿到之后立刻 ``zadd`` 建索引，让 §56 的 Reaper 能用区间扫描找到它。

        :return: 新的 :class:`~app.domain.models.LeaseRecord`；
            ``None`` = 已有执行者持有租约（调用方应当停下来去查状态，
            而不是排队硬等 —— 硬等会掩盖「上一个 Worker 已经死了」这个事实）。
        """
        ttl = int(ttl_seconds or self._ttl)
        now = self._clock.time()
        record = LeaseRecord(
            worker_id=worker_id,
            lease_id=new_lease_id(),
            call_id=call_id,
            expire_at=now + ttl,
            ttl_seconds=ttl,
        )
        acquired = self._redis.set(
            self._key(call_id), encode(record), nx=True, ex=ttl
        )
        if not acquired:
            return None
        # 索引的分数就是 expire_at —— Reaper 于是可以用一次 ZRANGEBYSCORE 捞出所有过期租约。
        self._redis.zadd(LEASE_ZSET, {call_id: record.expire_at})
        return record

    def renew(
        self,
        call_id: str,
        *,
        lease_id: str,
        worker_id: str,
        ttl_seconds: int | None = None,
    ) -> bool:
        """§15 续租（compare-and-renew）。

        :return: ``True`` = 续租成功，本 Worker 仍是合法执行者；
            ``False`` = **本 Worker 已失去执行权**，必须立刻停止写结果并抛
            :class:`~app.domain.errors.LeaseLost`。

        为什么是 False 而不是异常：:class:`~app.lease.heartbeat.WorkerHeartbeat`
        的后台线程要把它翻译成「停心跳 + 计数」，而 Gateway 的关键路径要把它翻译成
        「抛 LeaseLost 中止执行」——同一个信号两种处置，交给调用方决定更清晰。
        """
        ttl = int(ttl_seconds or self._ttl)
        ok = int(
            self._redis.eval_script(
                RENEW_SCRIPT,
                [self._key(call_id), LEASE_ZSET],
                [call_id, lease_id, worker_id, ttl],
            )
        )
        if not ok:
            logger.warning(
                "lease renew rejected (lost ownership): call=%s lease=%s worker=%s",
                call_id,
                lease_id,
                worker_id,
            )
        return ok == 1

    def release(self, call_id: str, *, lease_id: str) -> bool:
        """§15 归还租约（compare-and-release），并清掉 zset 索引。

        :return: ``True`` = 归还成功；``False`` = 租约不存在，或已经不是这把 lease
            （说明续租已经失败、租约被回收过 —— 调用方应当知道这件事）。
        """
        ok = int(
            self._redis.eval_script(
                RELEASE_SCRIPT, [self._key(call_id), LEASE_ZSET], [call_id, lease_id]
            )
        )
        return ok == 1

    def force_release(self, call_id: str) -> bool:
        """**不带身份校验**地回收租约 —— 只给 Reaper 用（§56）。

        为什么它可以不校验：Reaper 的调用前提是「这把租约已经过期」（它从
        :meth:`expired` 来），过期意味着没有任何合法持有者，也就没有人的所有权
        会被误伤。**业务路径绝不允许调用它** —— 那等于绕过 §15 的全部保护。
        """
        removed_key = self._redis.delete(self._key(call_id)) > 0
        removed_index = self._redis.zrem(LEASE_ZSET, call_id) > 0
        return removed_key or removed_index

    # ==================================================================
    # 读取 / 判活
    # ==================================================================
    def get(self, call_id: str) -> LeaseRecord | None:
        """读租约；不存在返回 ``None``。"""
        raw = self._redis.get(self._key(call_id))
        if raw is None:
            return None
        return LeaseRecord.from_json(raw)

    def is_alive(self, call_id: str) -> bool:
        """租约是否存在**且**尚未到期。

        两个条件都要：键还在但 ``expire_at <= now`` 是可能的 ——
        Redis TTL 与记录里的 ``expire_at`` 是两套机制（前者是淘汰，后者是判据），
        在手动时钟下尤其要显式比较，不能只信「键还在」。
        """
        record = self.get(call_id)
        if record is None:
            return False
        return record.expire_at > self._clock.time()

    # ==================================================================
    def expired(self, *, now: float | None = None) -> list[str]:
        """列出**已过期**的租约 call_id（§56 的输入）。

        两步走：

        1. ``ZRANGEBYSCORE(LEASE_ZSET, 0, now)`` —— 拿候选。这是 O(log N + M) 的
           区间查询，替代生产环境里绝对不允许的 ``KEYS`` 全量扫描。
        2. **双重核对**：对每个候选重新读一次租约键。zset 的分数可能滞后于实际键状态
           （例如键被 TTL 自然淘汰、或 release 删了键但索引更新失败），
           只信索引会把「其实还活着的租约」判死 —— 那正是 §56 最怕的误杀。

        判定规则：

        * 键已消失 → 计入过期。**Redis 的 TTL 已经替我们判了死刑**，
          索引里只是还没清掉的残影（Reaper 会在接管时 ``force_release`` 一并清掉）。
        * 键还在但 ``expire_at <= now`` → 计入过期（分数与记录都这么说才动手）。
        * 键还在且 ``expire_at > now`` → **不计入**（索引滞后，误杀纠正）。
        """
        at = self._clock.time() if now is None else now
        candidates = self._redis.zrangebyscore(LEASE_ZSET, 0, at)
        expired: list[str] = []
        for call_id in candidates:
            record = self.get(call_id)
            if record is None or record.expire_at <= at:
                expired.append(call_id)
        return expired


__all__ = ["LeaseManager", "RELEASE_SCRIPT", "RENEW_SCRIPT"]
