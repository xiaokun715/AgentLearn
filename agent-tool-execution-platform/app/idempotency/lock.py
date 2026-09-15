"""分布式锁 —— 让「同一件事只能有一个执行者」在 Redis 上成立（§9 / §10）。

职责
----
提供一把**带持有者身份、带 TTL、释放时校验持有者**的锁：

* §10 ``SET key value NX EX`` —— 「抢锁 / 没抢到」的全部竞争裁决由这一条命令完成，
  不需要 ``GET`` 再 ``SET`` 那种两段式（那中间一定有窗口）。
* §9 释放必须校验持有者 —— 用 Lua 脚本（此处由 :meth:`RedisSim.register_script`
  注册等价 Python 实现）读出 value 里的 ``owner``，只有与自己一致才 ``DEL``。

为什么不能用裸 ``DEL``
----------------------
这是分布式锁最经典的坑，值得写清楚::

    t0  A 抢到锁（TTL=30s），准备执行一个耗时 40s 的操作
    t30 锁自动过期（A 还活着，只是慢）
    t31 B 抢到了同一把锁，开始执行
    t40 A 执行完了，执行 finally 里的 DEL —— 它删掉的是 **B 的锁**
    t41 C 也抢到了锁，于是 B 和 C 同时在执行

根因：A 的 ``DEL`` 没有证明「这把锁还是我加的」。所以释放必须是
「compare-and-delete」——比较 value 里的 owner，一致才删。同一条规则也适用于
续期（:mod:`app.lease.manager` 的 ``lease_renew``，那里比较的是 ``lease_id``）。

为什么整个包都是同步（sync）的
------------------------------
:class:`~app.infra.redis.RedisSim` 是同步的，单线程进程内同步方法之间不会发生
协程切换，因此每个命令天然原子 —— 这与真实 Redis「单线程串行执行命令」的模型
一致。所以这里注册的脚本在模拟器与真机上语义相同；**任何 await 都会破坏这个前提**，
绝不允许混入。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from ..infra.clock import Clock
from ..infra.redis import RedisSim, decode, encode, tool_lock_key

logger = logging.getLogger(__name__)

RELEASE_SCRIPT = "lock_release"
"""释放脚本名：compare-and-delete（§9）。"""


@dataclass(frozen=True)
class LockInfo:
    """锁 value 的结构化视图。

    存这三个字段是有讲究的：

    * ``owner``      —— 释放 / 判定持有者时比较的依据（§9）
    * ``acquired_at``—— 排障时能算出「这把锁被持有了多久」，判断是否卡死
    * ``expire_at``  —— 不依赖 Redis TTL 也能判断锁是否已过期（TTL 是对账依据，不是唯一依据）

    ``expire_at`` 用 epoch 秒而不是 datetime，是为了和 Lua 脚本里直接比较时间戳的
    写法保持一致（§15 对 :class:`~app.domain.models.LeaseRecord` 也是同款要求）。
    """

    owner: str
    acquired_at: float
    expire_at: float

    def to_payload(self) -> str:
        """编成 Redis value（``encode`` 支持 dict / Pydantic）。"""
        return encode(
            {
                "owner": self.owner,
                "acquired_at": self.acquired_at,
                "expire_at": self.expire_at,
            }
        )

    @classmethod
    def from_payload(cls, raw: str | None) -> "LockInfo | None":
        """从 Redis value 还原；``None`` / 脏数据一律返回 ``None``。

        注意：这里对脏数据是**宽松**的（返回 ``None`` 表示「没锁」），
        与 :mod:`app.idempotency.state` 对幂等记录**严格**的解析策略刻意相反 ——
        锁读不出来最多多抢一次（有 TTL 兜底），幂等记录读不出来则可能导致重复执行。
        """
        data = decode(raw)
        if not isinstance(data, dict) or "owner" not in data:
            return None
        return cls(
            owner=str(data.get("owner", "")),
            acquired_at=float(data.get("acquired_at", 0.0) or 0.0),
            expire_at=float(data.get("expire_at", 0.0) or 0.0),
        )


def _script_release(redis: RedisSim, keys: list[str], args: list[Any]) -> int:
    """``lock_release`` 的脚本体：compare-and-delete。

    真实项目里这段是 Lua，由 Redis 端串行执行；此处是等价的同步 Python 实现，
    原子性一致（见模块 docstring 的「为什么整个包都是同步的」）。

    :return: ``1`` = 释放成功；``0`` = 锁不存在，或持有者不是调用方（**都不是错误**，
        后者恰恰说明本进程已经失去了锁，调用方应当据此停止后续写操作）
    """
    lock_key = keys[0]
    owner = args[0]
    info = LockInfo.from_payload(redis.get(lock_key))
    if info is None:
        return 0
    if info.owner != owner:
        # 关键分支：A 超时释放后 B 拿到了锁，A 恢复后走到这里 → 绝不能删 B 的锁。
        return 0
    redis.delete(lock_key)
    return 1


class DistributedLock:
    """基于 ``SET NX EX`` + compare-and-delete 的分布式锁。

    使用方式二选一::

        # 1) 显式加锁/解锁
        lock = DistributedLock(redis, ttl_seconds=30)
        if lock.acquire("recovery:call_1", owner="reaper-1"):
            try:
                ...
            finally:
                lock.release("recovery:call_1", owner="reaper-1")

        # 2) 上下文管理器（推荐，异常路径也不会漏掉释放）
        with lock.hold("recovery:call_1", owner="reaper-1") as got:
            if got:
                ...

    锁的粒度建议：**一把锁保护一个聚合根**（一个 call、一次恢复），
    而不是「一个大锁保护所有东西」—— 后者会把并发度压成 1，还放大死锁面。
    """

    def __init__(
        self, redis: RedisSim, *, ttl_seconds: int = 30, clock: Clock | None = None
    ) -> None:
        self._redis = redis
        # 缺省与 RedisSim 共用同一个时钟：否则脚本里用 Redis 时间算 TTL、
        # Python 侧用另一个时钟算 expire_at，两边会各算各的，ManualClock 演示直接失真。
        self._clock: Clock = clock or redis.clock
        self._ttl_seconds = ttl_seconds
        # 注册脚本（重复注册只是覆盖同名实现，语义相同，天然幂等）
        redis.register_script(RELEASE_SCRIPT, _script_release)

    # ==================================================================
    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def _lock_key(self, key: str) -> str:
        """统一加 ``tool_lock:`` 前缀，避免与幂等键 / 租约键撞名（§43 键空间隔离）。"""
        return tool_lock_key(key)

    def _payload(self, owner: str) -> str:
        """每次抢锁都重新生成 payload：``acquired_at`` 必须是**这次**的尝试时间。"""
        now = self._clock.time()
        return LockInfo(
            owner=owner, acquired_at=now, expire_at=now + self._ttl_seconds
        ).to_payload()

    # ==================================================================
    # 抢锁
    # ==================================================================
    def acquire(
        self,
        key: str,
        *,
        owner: str,
        blocking: bool = False,
        timeout_seconds: float = 0.0,
        poll_interval: float = 0.02,
    ) -> bool:
        """尝试获取锁。

        :param owner: 持有者标识（如 ``worker-3`` / ``reaper-1``）。释放时要比对它。
        :param blocking: ``True`` 时在 ``timeout_seconds`` 内轮询重试。
        :param timeout_seconds: 阻塞等待的上限；``0`` 表示「只试一次」。
        :param poll_interval: 轮询间隔。
        :return: 是否拿到锁。

        等待用 ``self._clock.sleep`` 而不是 ``time.sleep``：``ManualClock`` 下
        「等待 30 秒」会变成「逻辑时间前进 30 秒」，演示与推演都不必真等。
        """
        lock_key = self._lock_key(key)
        if self._redis.set(lock_key, self._payload(owner), nx=True, ex=self._ttl_seconds):
            return True

        if not blocking or timeout_seconds <= 0:
            return False

        deadline = self._clock.time() + timeout_seconds
        while self._clock.time() < deadline:
            self._clock.sleep(poll_interval)
            # 每轮都重新 SET NX：抢锁本身是原子的，无需额外判空（判空反而引入窗口）
            if self._redis.set(
                lock_key, self._payload(owner), nx=True, ex=self._ttl_seconds
            ):
                return True
        return False

    # ==================================================================
    # 释放 / 查询
    # ==================================================================
    def release(self, key: str, *, owner: str) -> bool:
        """compare-and-delete 释放锁。

        :return: ``True`` = 确实是我释放的；``False`` = 锁已不在，或已经不是我的锁。
            ``False`` **不是异常**，但调用方应当把它当成一个信号：
            「本次临界区可能已经超时，别人接管了」，后续的写操作要停下来重新确认。
        """
        released = int(
            self._redis.eval_script(
                RELEASE_SCRIPT, [self._lock_key(key)], [owner]
            )
        )
        if not released:
            logger.debug("lock not released (owner mismatch or absent): %s owner=%s", key, owner)
        return bool(released)

    def is_held_by(self, key: str, owner: str) -> bool:
        """当前锁是否由 ``owner`` 持有（不判断是否已过期，过期由 TTL 负责）。"""
        info = LockInfo.from_payload(self._redis.get(self._lock_key(key)))
        return info is not None and info.owner == owner

    def info(self, key: str) -> LockInfo | None:
        """读锁的元信息 —— 排障用（谁持有的、什么时候到期的）。"""
        return LockInfo.from_payload(self._redis.get(self._lock_key(key)))

    def ttl(self, key: str) -> int:
        """剩余秒数：``-1`` 无过期，``-2`` 锁不存在。"""
        return self._redis.ttl(self._lock_key(key))

    # ==================================================================
    @contextmanager
    def hold(
        self,
        key: str,
        *,
        owner: str,
        blocking: bool = False,
        timeout_seconds: float = 0.0,
    ) -> Iterator[bool]:
        """上下文管理器：``yield`` 是否抢到锁；**抢到才在 finally 里释放**。

        为什么 yield 布尔值而不是抢不到就抛异常：抢不到锁是很常见的正常分支
        （另一个 Reaper 正在处理同一个 call），让调用方自己决定「跳过」比
        用异常控制流程清晰得多 —— 见 :meth:`app.lease.reaper.LeaseReaper.scan_once`。
        """
        acquired = self.acquire(
            key, owner=owner, blocking=blocking, timeout_seconds=timeout_seconds
        )
        try:
            yield acquired
        finally:
            if acquired:
                self.release(key, owner=owner)


__all__ = ["DistributedLock", "LockInfo", "RELEASE_SCRIPT"]
