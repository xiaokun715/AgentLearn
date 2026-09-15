"""Redis 模拟器 —— 平台的「高性能协调层」（说明书 §43）。

为什么要有这一层
----------------
说明书 §43/§44 把职责切得很清楚：

* **PostgreSQL** = Durable Source of Truth（快照、结果、审计，见 :mod:`app.infra.database`）
* **Redis** = 高性能协调层（幂等键、租约、锁、心跳、队列）

协调层是**并发正确性**的主战场：``SET NX EX`` 抢执行权（§10）、
``lease_id`` compare-and-renew（§15）、分布式锁释放时校验持有者（§9）。
这些操作的正确性无一例外依赖「读-改-写」的原子性。

因此本模拟器做了一件关键的事：**所有命令都是同步的**。
在单线程进程里，同步方法之间不会发生协程切换，于是每个命令天然原子 ——
这与真实 Redis「单线程串行执行命令」的模型是一致的，
所以 :meth:`RedisSim.eval_script` 里注册的脚本在模拟器与真机上语义相同。

一个诚实的提醒：真机上「客户端每次读都可能拿到过期视图」，而本模拟器同进程共享内存。
但所有**关键状态迁移**都收敛到了脚本里（见 ``app/idempotency`` 与 ``app/lease``），
只要不绕开脚本直接读改写，行为就对得上。
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .clock import Clock, SystemClock


class WrongType(Exception):
    """``WRONGTYPE`` —— 键已存在且类型不符。模拟器也保留这个错误，避免写出真机上才炸的代码。"""


@dataclass
class _Entry:
    """字符串键的值 + 过期时间（epoch 秒；``None`` 表示永不过期）。"""

    value: str
    expire_at: Optional[float] = None


@dataclass
class _StreamGroup:
    """Stream 的消费者组：只需记录「哪些消息已被投递、谁在读」。

    ``pending`` 是 Redis 术语里的 PEL（Pending Entries List）——
    **这正是 Worker 崩溃恢复的关键数据结构**：消息投递出去但没 ACK，
    就躺在 PEL 里等着被 ``XAUTOCLAIM`` 捞回来（§56 Worker Crash Recovery）。
    """

    name: str
    last_delivered_id: str = "0-0"
    pending: dict[str, tuple[str, float]] = field(default_factory=dict)
    # msg_id -> (consumer_name, delivered_at_epoch)


@dataclass
class _Stream:
    entries: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    last_id: int = 0
    groups: dict[str, _StreamGroup] = field(default_factory=dict)


ScriptFn = Callable[["RedisSim", list[str], list[Any]], Any]


class RedisSim:
    """一个够用、够像的 Redis 子集。

    支持的数据结构：String / Hash / List / ZSet / Stream。
    支持 ``SET NX EX``、TTL、以及注册式 Lua 脚本（原子执行）。
    """

    def __init__(self, clock: Optional[Clock] = None) -> None:
        self.clock = clock or SystemClock()
        self._strings: dict[str, _Entry] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._hash_expire: dict[str, float] = {}
        self._lists: dict[str, list[str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._streams: dict[str, _Stream] = {}
        self._scripts: dict[str, ScriptFn] = {}
        self._stream_seq = 0

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _purge_expired(self, key: str) -> None:
        """惰性过期：访问时才发现过期，行为与 Redis 的 lazy expire 一致。"""
        now = self.clock.time()
        entry = self._strings.get(key)
        if entry is not None and entry.expire_at is not None and entry.expire_at <= now:
            self._strings.pop(key, None)
        exp = self._hash_expire.get(key)
        if exp is not None and exp <= now:
            self._hash_expire.pop(key, None)
            self._hashes.pop(key, None)

    def _existing_types(self, key: str) -> set[str]:
        self._purge_expired(key)
        found = set()
        if key in self._strings:
            found.add("string")
        if key in self._hashes:
            found.add("hash")
        if key in self._lists:
            found.add("list")
        if key in self._zsets:
            found.add("zset")
        if key in self._streams:
            found.add("stream")
        return found

    def _assert_type(self, key: str, expected: str) -> None:
        kinds = self._existing_types(key)
        if kinds and expected not in kinds:
            raise WrongType(
                f"WRONGTYPE Operation against a key holding the wrong kind of value: {key}"
            )

    def _exists_alive(self, key: str) -> bool:
        return bool(self._existing_types(key))

    # ==================================================================
    # String
    # ==================================================================
    def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: Optional[float] = None,
        px: Optional[float] = None,
    ) -> bool:
        """``SET key value [NX] [XX] [EX s] [PX ms]`` —— §10 原子创建的核心。

        ``nx=True`` 时，只有键不存在才能写成功：这一条命令就是
        「抢到执行权 / 没抢到」的全部竞争裁决。
        """
        self._assert_type(key, "string")
        fresh = not self._exists_alive(key)
        if nx and not fresh:
            return False
        if xx and fresh:
            return False
        ttl: Optional[float] = None
        if ex is not None:
            ttl = self.clock.time() + float(ex)
        elif px is not None:
            ttl = self.clock.time() + float(px) / 1000.0
        self._strings[key] = _Entry(str(value), ttl)
        return True

    def get(self, key: str) -> Optional[str]:
        self._purge_expired(key)
        entry = self._strings.get(key)
        return entry.value if entry else None

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            for store in (self._strings, self._hashes, self._lists, self._zsets, self._streams):
                if key in store:
                    store.pop(key, None)
                    removed += 1
                    break
            self._hash_expire.pop(key, None)
        return removed

    def exists(self, *keys: str) -> int:
        return sum(1 for k in keys if self._exists_alive(k))

    def expire(self, key: str, seconds: float) -> bool:
        """给 String / Hash 键续期。返回 ``True`` 表示键存在且已设置。"""
        self._purge_expired(key)
        at = self.clock.time() + float(seconds)
        if key in self._strings:
            self._strings[key].expire_at = at
            return True
        if key in self._hashes:
            self._hash_expire[key] = at
            return True
        return False

    def ttl(self, key: str) -> int:
        """剩余秒数：``-1`` 无过期，``-2`` 键不存在。"""
        self._purge_expired(key)
        entry = self._strings.get(key)
        at = entry.expire_at if entry else self._hash_expire.get(key)
        if at is None:
            return -1 if self._exists_alive(key) else -2
        return max(0, int(at - self.clock.time()))

    def incr(self, key: str, amount: int = 1) -> int:
        self._purge_expired(key)
        entry = self._strings.get(key)
        current = int(entry.value) if entry else 0
        new = current + amount
        self._strings[key] = _Entry(str(new), entry.expire_at if entry else None)
        return new

    # ==================================================================
    # Hash —— 心跳、租约、状态快照常用
    # ==================================================================
    def hset(self, key: str, mapping: dict[str, Any]) -> int:
        self._assert_type(key, "hash")
        bucket = self._hashes.setdefault(key, {})
        added = 0
        for field_name, value in mapping.items():
            if field_name not in bucket:
                added += 1
            bucket[field_name] = str(value)
        return added

    def hget(self, key: str, field_name: str) -> Optional[str]:
        self._purge_expired(key)
        return self._hashes.get(key, {}).get(field_name)

    def hgetall(self, key: str) -> dict[str, str]:
        self._purge_expired(key)
        return dict(self._hashes.get(key, {}))

    def hdel(self, key: str, *fields: str) -> int:
        bucket = self._hashes.get(key)
        if not bucket:
            return 0
        removed = 0
        for f in fields:
            if bucket.pop(f, None) is not None:
                removed += 1
        if not bucket:
            self._hashes.pop(key, None)
        return removed

    def hincrby(self, key: str, field_name: str, amount: int = 1) -> int:
        self._assert_type(key, "hash")
        bucket = self._hashes.setdefault(key, {})
        new = int(bucket.get(field_name, "0")) + amount
        bucket[field_name] = str(new)
        return new

    # ==================================================================
    # List —— 简单队列（生产可换 Redis Streams consumer group）
    # ==================================================================
    def lpush(self, key: str, *values: str) -> int:
        self._assert_type(key, "list")
        bucket = self._lists.setdefault(key, [])
        for v in values:
            bucket.insert(0, str(v))
        return len(bucket)

    def rpush(self, key: str, *values: str) -> int:
        self._assert_type(key, "list")
        bucket = self._lists.setdefault(key, [])
        bucket.extend(str(v) for v in values)
        return len(bucket)

    def lpop(self, key: str) -> Optional[str]:
        self._assert_type(key, "list")
        bucket = self._lists.get(key)
        if not bucket:
            return None
        value = bucket.pop(0)
        if not bucket:
            self._lists.pop(key, None)
        return value

    def rpop(self, key: str) -> Optional[str]:
        self._assert_type(key, "list")
        bucket = self._lists.get(key)
        if not bucket:
            return None
        value = bucket.pop()
        if not bucket:
            self._lists.pop(key, None)
        return value

    def lrange(self, key: str, start: int = 0, end: int = -1) -> list[str]:
        bucket = self._lists.get(key, [])
        if end == -1:
            return list(bucket[start:])
        return list(bucket[start : end + 1])

    def llen(self, key: str) -> int:
        return len(self._lists.get(key, []))

    # ==================================================================
    # ZSet —— 延迟队列 / 定时扫描（Lease Reaper 用）
    # ==================================================================
    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self._assert_type(key, "zset")
        bucket = self._zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in bucket:
                added += 1
            bucket[member] = float(score)
        return added

    def zrange(self, key: str, start: int = 0, end: int = -1, *, withscores: bool = False):
        bucket = self._zsets.get(key, {})
        ordered = sorted(bucket.items(), key=lambda kv: (kv[1], kv[0]))
        sliced = ordered[start:] if end == -1 else ordered[start : end + 1]
        if withscores:
            return [(m, s) for m, s in sliced]
        return [m for m, _ in sliced]

    def zrangebyscore(self, key: str, min_score: float, max_score: float) -> list[str]:
        """按分数区间取成员 —— Reaper 用它捞「``expire_at <= now``」的过期租约。"""
        bucket = self._zsets.get(key, {})
        return [
            m
            for m, s in sorted(bucket.items(), key=lambda kv: (kv[1], kv[0]))
            if min_score <= s <= max_score
        ]

    def zrem(self, key: str, *members: str) -> int:
        bucket = self._zsets.get(key)
        if not bucket:
            return 0
        removed = 0
        for m in members:
            if bucket.pop(m, None) is not None:
                removed += 1
        if not bucket:
            self._zsets.pop(key, None)
        return removed

    def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, {}))

    def keys(self, pattern: str = "*") -> list[str]:
        found: list[str] = []
        for store in (self._strings, self._hashes, self._lists, self._zsets, self._streams):
            found.extend(store.keys())
        return [k for k in found if fnmatch.fnmatch(k, pattern)]

    # ==================================================================
    # Stream —— Worker 队列 + 崩溃重投递（§56 的数据基础）
    # ==================================================================
    def xadd(self, key: str, fields: dict[str, Any]) -> str:
        """追加一条消息，返回自增 ID ``<ms>-<seq>``。"""
        stream = self._streams.setdefault(key, _Stream())
        self._stream_seq += 1
        msg_id = f"{int(self.clock.time() * 1000)}-{self._stream_seq}"
        stream.entries.append((msg_id, {k: str(v) for k, v in fields.items()}))
        stream.last_id = self._stream_seq
        return msg_id

    def xlen(self, key: str) -> int:
        return len(self._streams.get(key, _Stream()).entries)

    def xgroup_create(self, key: str, group: str, *, mkstream: bool = True) -> bool:
        stream = self._streams.setdefault(key, _Stream())
        if group in stream.groups:
            return False
        stream.groups[group] = _StreamGroup(name=group, last_delivered_id="0-0")
        return True

    def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: dict[str, str],
        *,
        count: int = 1,
    ) -> list[tuple[str, dict[str, str]]]:
        """从消费者组读新消息（``>`` 语义），并把它们记入该消费者的 PEL。

        返回 ``[(msg_id, fields), ...]``；没有新消息时返回空列表（非阻塞）。
        """
        out: list[tuple[str, dict[str, str]]] = []
        for key in streams:
            stream = self._streams.get(key)
            if stream is None or group not in stream.groups:
                continue
            grp = stream.groups[group]
            for msg_id, fields in stream.entries:
                if len(out) >= count:
                    break
                if self._stream_id_gt(msg_id, grp.last_delivered_id):
                    grp.last_delivered_id = msg_id
                    grp.pending[msg_id] = (consumer, self.clock.time())
                    out.append((msg_id, dict(fields)))
        return out

    def xack(self, key: str, group: str, *msg_ids: str) -> int:
        """确认消费完成 —— 从 PEL 移除，消息才算真正落地。"""
        stream = self._streams.get(key)
        if stream is None or group not in stream.groups:
            return 0
        grp = stream.groups[group]
        acked = 0
        for msg_id in msg_ids:
            if grp.pending.pop(msg_id, None) is not None:
                acked += 1
        return acked

    def xpending(self, key: str, group: str) -> list[tuple[str, str, float]]:
        """列出 PEL：``[(msg_id, consumer, idle_seconds), ...]``。"""
        stream = self._streams.get(key)
        if stream is None or group not in stream.groups:
            return []
        now = self.clock.time()
        return [
            (msg_id, consumer, max(0.0, now - delivered_at))
            for msg_id, (consumer, delivered_at) in stream.groups[group].pending.items()
        ]

    def xinfo_groups(self, key: str) -> list[dict[str, Any]]:
        """``XINFO GROUPS`` 的等价物：每个消费者组的投递进度与待处理量。

        为什么需要它：``XLEN`` 返回的是 Stream 里**累计**的条目数，
        已消费的条目并不会消失（Redis 不像 List 那样弹出即删）。
        所以 ``XLEN`` 不能回答「还有多少任务没做」——
        那个数是「ID 大于 ``last_delivered_id`` 的条目数」。
        把这两者混为一谈，监控面板上就会显示一条永远不下降的队列长度。
        """
        stream = self._streams.get(key)
        if stream is None:
            return []
        out: list[dict[str, Any]] = []
        for name, grp in stream.groups.items():
            unconsumed = sum(
                1 for msg_id, _ in stream.entries
                if self._stream_id_gt(msg_id, grp.last_delivered_id)
            )
            out.append(
                {
                    "name": name,
                    "last_delivered_id": grp.last_delivered_id,
                    "pending": len(grp.pending),
                    "unconsumed": unconsumed,
                }
            )
        return out

    def xautoclaim(
        self,
        key: str,
        group: str,
        consumer: str,
        *,
        min_idle_ms: float,
        count: int = 10,
    ) -> list[tuple[str, dict[str, str]]]:
        """接管「投递出去超过 ``min_idle_ms`` 仍未 ACK」的消息（§56）。

        这是 Worker 崩溃恢复的**接收侧**：Worker 死了，它那条 PEL 记录会在
        idle 超过阈值后被活着的 Worker 领走重做。是否真的能重做，
        还要看 Tool 的 :class:`~app.domain.enums.IdempotencyLevel`（§57）。
        """
        claimed: list[tuple[str, dict[str, str]]] = []
        stream = self._streams.get(key)
        if stream is None or group not in stream.groups:
            return claimed
        grp = stream.groups[group]
        now = self.clock.time()
        lookup = dict(stream.entries)
        for msg_id, (owner, delivered_at) in list(grp.pending.items()):
            if len(claimed) >= count:
                break
            if owner == consumer:
                continue
            if (now - delivered_at) * 1000.0 < min_idle_ms:
                continue
            grp.pending[msg_id] = (consumer, now)
            if msg_id in lookup:
                claimed.append((msg_id, dict(lookup[msg_id])))
        return claimed

    @staticmethod
    def _stream_id_gt(a: str, b: str) -> bool:
        """比较 Stream ID（``ms-seq``），先比 ms 再比 seq。"""

        def parse(s: str) -> tuple[int, int]:
            head, _, tail = s.partition("-")
            return int(head or 0), int(tail or 0)

        return parse(a) > parse(b)

    # ==================================================================
    # Lua 脚本（§10 / §15 的原子性来源）
    # ==================================================================
    def register_script(self, name: str, fn: ScriptFn) -> None:
        """注册一个具名脚本。

        真实项目里 ``fn`` 是 Lua 源码字符串，由 Redis 端串行执行；
        这里换成等价的 Python 实现 —— **单线程 + 同步**，原子性与真机一致。
        好处是脚本里的复合逻辑（比较 lease_id 再续期）可以用惯用 Python 写，
        同时把「不允许出现 await 」这条约束显式化。
        """
        self._scripts[name] = fn

    def eval_script(self, name: str, keys: Optional[list[str]] = None, args: Optional[list[Any]] = None) -> Any:
        """执行已注册脚本，返回脚本返回值。"""
        fn = self._scripts.get(name)
        if fn is None:
            raise KeyError(f"script not registered: {name}")
        return fn(self, keys or [], args or [])

    # ==================================================================
    # 运维辅助
    # ==================================================================
    def flushall(self) -> None:
        self._strings.clear()
        self._hashes.clear()
        self._hash_expire.clear()
        self._lists.clear()
        self._zsets.clear()
        self._streams.clear()

    def dbsize(self) -> int:
        """当前存活键数量（诊断用）。"""
        self._purge_all()
        return sum(
            len(s) for s in (self._strings, self._hashes, self._lists, self._zsets, self._streams)
        )

    def _purge_all(self) -> None:
        for key in list(self._strings.keys()):
            self._purge_expired(key)
        for key in list(self._hashes.keys()):
            self._purge_expired(key)

    def dump(self) -> dict[str, Any]:
        """导出全部状态 —— 审计/排障用，把协调层当前的样子一次性看清。"""
        self._purge_all()
        return {
            "strings": {k: v.value for k, v in self._strings.items()},
            "hashes": {k: dict(v) for k, v in self._hashes.items()},
            "lists": {k: list(v) for k, v in self._lists.items()},
            "zsets": {k: dict(v) for k, v in self._zsets.items()},
            "streams": {
                k: {
                    "len": len(v.entries),
                    "groups": {
                        g: {
                            "last_delivered_id": grp.last_delivered_id,
                            "pending": dict(grp.pending),
                        }
                        for g, grp in v.groups.items()
                    },
                }
                for k, v in self._streams.items()
            },
        }


# ======================================================================
# Redis 键命名空间（说明书 §43）
# ======================================================================
def idempotency_key(key: str) -> str:
    return f"idempotency:{key}"


def lease_key(call_id: str) -> str:
    return f"lease:{call_id}"


def agent_heartbeat_key(agent_id: str, run_id: str) -> str:
    return f"agent_heartbeat:{agent_id}:{run_id}"


def tool_lock_key(key: str) -> str:
    return f"tool_lock:{key}"


def tool_status_key(call_id: str) -> str:
    return f"tool_status:{call_id}"


def approval_key(call_id: str) -> str:
    return f"approval:{call_id}"


LEASE_ZSET = "lease:expirations"
"""按 ``expire_at`` 排序的租约索引 —— Reaper 用它做「谁的租约过期了」的区间扫描（§56）。"""

JOB_STREAM = "tool_jobs"
"""异步任务队列（§4.2 / §69 Redis Streams）。"""

JOB_GROUP = "workers"


def encode(record: Any) -> str:
    """把 Pydantic 记录 / dict 编成 Redis value。"""
    if hasattr(record, "model_dump_json"):
        return record.model_dump_json()
    return json.dumps(record, ensure_ascii=False, default=str)


def decode(raw: Optional[str]) -> Optional[dict]:
    """读回来的 JSON dict；``None`` 透传。"""
    if raw is None:
        return None
    return json.loads(raw)


def iter_batches(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    """小工具：把可迭代对象切成固定大小的批次。"""
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
