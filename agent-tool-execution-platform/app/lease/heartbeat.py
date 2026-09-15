"""两类心跳 —— 分清「Tool 还活着」与「Agent 还活着」（§15 / §16 / §17）。

两件事常被混为一谈，但它们的**判据**与**处置**完全不同::

    ┌──────────────────┬────────────────────────┬──────────────────────────────┐
    │                  │ WorkerHeartbeat (§15)  │ AgentHeartbeat (§16/§17)     │
    ├──────────────────┼────────────────────────┼──────────────────────────────┤
    │ 证明谁活着        │ 执行 Tool 的 Worker    │ 提交 Tool 的 Agent /Workflow │
    │ 载体              │ 续租 lease:{call_id}   │ agent_heartbeat:{a}:{run}    │
    │ 失败后果          │ 执行权被 Reaper 回收    │ **Agent 不会再回来取结果**    │
    │ 处置              │ LeaseLost -> 停止写结果 │ 继续执行 / 取消 / 转人工（§17）│
    └──────────────────┴────────────────────────┴──────────────────────────────┘

§15 为什么是 ``TTL = 30s`` + ``Heartbeat = 10s``
------------------------------------------------
参数不是随手定的，它决定「容忍几次丢包」：

* 心跳间隔 10s，租约 30s → **连续丢两次心跳（20s）仍然不失联**，第三次（30s）才判死。
  留出一个完整周期的余量，是为了容忍一次网络抖动 / 一次 GC 停顿 / 一次调度延迟，
  而不是「一次抖动就换执行者」。
* 反过来，心跳间隔**不能**接近 TTL：那样每次轻微延迟都会触发误判，
  系统会在健康节点之间反复迁移执行权，产生大量不必要的重复执行。
* :class:`~app.config.AppConfig` 里这两个值都可配（``for_demo`` 会缩到 5s / 1s，
  让「租约过期 -> Reaper 接管」在演示里几秒内发生）。

§17 为什么需要 Agent 心跳
-------------------------
Worker 心跳回答了「Tool 在跑吗」，但回答不了「**还要不要它跑**」。
考虑一个异步 Tool 提交之后的场景::

    Agent 提交 run_test（预计 5 分钟）-> 平台接受并入队 -> Agent 崩溃 / 用户关掉了页面

    如果没有 Agent 心跳，平台只能等到 Tool 跑完 —— 5 分钟的集群资源被烧掉，
    而结果送到一个已经死掉的 Agent 手里，永远不会有人来取。
    Agent 心跳让平台有能力**在这 5 分钟里**做出判断（§17）：

    * **non-destructive（可安全继续）**：读操作 / 纯计算 —— 让它跑完，
      结果落库，等 Agent 恢复或人工取走，成本只是一点资源。
    * **expensive（该取消）**：`run_test` 这类重资源任务 —— Agent 都走了，
      继续跑纯粹是浪费，主动 CANCELLING -> CANCELLED。
    * **dangerous（要转人工）**：高风险 / 有副作用的 Tool —— **绝不自动取消，也绝不
      自动继续猜测**，转人工确认（WAITING_HUMAN）。因为「取消」本身也是一个状态变更，
      半途取消一个已部分生效的副作用，比让它跑完更难收拾。

一句话：**Worker 心跳影响「能不能继续写结果」，Agent 心跳影响「还要不要这个结果」。**

为什么是同步（sync）
--------------------
心跳线程内部调用的仍是同步的 :meth:`LeaseManager.renew` / ``RedisSim.hset``。
单线程内同步方法之间不发生协程切换，每个命令天然原子，与真实 Redis 单线程串行
执行模型一致。线程只负责「按时触发」，**不引入任何 async/await**。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from ..config import AppConfig
from ..infra.clock import Clock
from ..infra.redis import RedisSim, agent_heartbeat_key
from .manager import LeaseManager

logger = logging.getLogger(__name__)


@dataclass
class _BeatState:
    """一个 call 的心跳线程状态。

    用 ``stop_event`` 而不是 ``while running`` 布尔量：``Event.wait(interval)``
    既是「定时」又是「可被立刻打断的等待」—— ``stop()`` 一置位，线程马上醒来退出，
    不必等满一个心跳周期，线程也不需要真的 ``join`` 很久。
    """

    call_id: str
    lease_id: str
    worker_id: str
    interval_seconds: float
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    beats: int = 0
    missed: int = 0
    lost: bool = False
    last_error: str | None = None


class WorkerHeartbeat:
    """Worker 心跳：定期续租，证明 Tool 正在执行（§15）。

    它是「执行权」的心跳 —— :meth:`beat` 本质上就是
    :meth:`~app.lease.manager.LeaseManager.renew` 的定时器包装。
    """

    def __init__(
        self,
        lease_manager: LeaseManager,
        *,
        interval_seconds: float,
        ttl_seconds: int,
    ) -> None:
        self._lease = lease_manager
        self._interval = float(interval_seconds)
        self._ttl = int(ttl_seconds)
        self._states: dict[str, _BeatState] = {}
        self._lock = threading.RLock()

    # ==================================================================
    @property
    def interval_seconds(self) -> float:
        return self._interval

    def beat(self, call_id: str, *, lease_id: str, worker_id: str) -> bool:
        """手动打一次心跳（= 续租一次）。后台线程也走这里。

        :return: ``True`` = 续租成功，本 Worker 仍是合法执行者。

        续租失败（CAS 失败）的含义非常重：**本 Worker 已经不是租约持有者**——
        要么租约过期后被 Reaper 判死、要么已经被另一个 Worker 接管。
        此时任何「继续执行 + 写结果」的动作都可能覆盖接管者的成果，
        §15 要求立刻停下来（调用方据此抛
        :class:`~app.domain.errors.LeaseLost`）。
        """
        ok = self._lease.renew(
            call_id, lease_id=lease_id, worker_id=worker_id, ttl_seconds=self._ttl
        )
        with self._lock:
            state = self._states.get(call_id)
            if state is None:
                # 允许「没 start 就直接 beat」（测试 / 短任务一次性续租）；
                # 此时也把状态登记下来，让 missed() 有数可查。
                state = _BeatState(
                    call_id=call_id,
                    lease_id=lease_id,
                    worker_id=worker_id,
                    interval_seconds=self._interval,
                )
                self._states[call_id] = state
            state.beats += 1
            if not ok:
                # 记 missed 并置位停止标志：CAS 失败后继续打心跳毫无意义，
                # 只会刷一堆「续租被拒」的日志，还掩盖了真正需要处理的 LeaseLost。
                state.missed += 1
                state.lost = True
                state.stop_event.set()
        if not ok:
            logger.warning(
                "heartbeat lost lease: call=%s lease=%s worker=%s",
                call_id,
                lease_id,
                worker_id,
            )
        return ok

    def start(self, call_id: str, *, lease_id: str, worker_id: str) -> None:
        """起一个 daemon 线程，每 ``interval_seconds`` 续租一次。

        为什么用 ``daemon=True``：进程退出时不该被心跳线程吊住 ——
        心跳是「维持性」工作，不是「必须完成」工作，进程退出时丢掉它是可接受的
        （租约会在 TTL 之后自然过期，Reaper 会按 §56 处理）。

        为什么续租失败就**停止线程**而不是重试：见 :meth:`beat` ——
        失去执行权是终局，不是瞬时故障。
        """
        with self._lock:
            state = self._states.get(call_id)
            if state is not None and state.thread is not None and state.thread.is_alive():
                return  # 已经在跑了，重复 start 是幂等的
            state = _BeatState(
                call_id=call_id,
                lease_id=lease_id,
                worker_id=worker_id,
                interval_seconds=self._interval,
            )
            self._states[call_id] = state
            thread = threading.Thread(
                target=self._loop,
                args=(state,),
                name=f"heartbeat-{call_id}",
                daemon=True,
            )
            state.thread = thread
        thread.start()

    def _loop(self, state: _BeatState) -> None:
        """心跳循环：``stop_event.wait`` 既定时又可被打断。

        注意：``wait`` 用的是**真实**时间（线程本来就跑在真实时间里）。
        用 :class:`~app.infra.clock.ManualClock` 做推演时，请直接调用 :meth:`beat`
        而不是依赖线程 —— 逻辑时间不会自己流逝，这是刻意的设计：
        「租约过期」必须由测试显式 ``advance``，而不是被后台线程偷偷触发。
        """
        while not state.stop_event.wait(state.interval_seconds):
            try:
                if not self.beat(
                    state.call_id,
                    lease_id=state.lease_id,
                    worker_id=state.worker_id,
                ):
                    return  # 已失去执行权，停心跳
            except Exception as exc:  # pragma: no cover - 防御：心跳线程不能无声死掉
                with self._lock:
                    state.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("heartbeat loop error: call=%s", state.call_id)
                return

    def stop(self, call_id: str) -> None:
        """停止某个 call 的心跳线程（正常收尾时调用）。

        状态**保留**在 ``_states`` 里不删：``missed()`` / ``beats`` 在停止之后
        仍然要可查 —— 执行收尾的审计逻辑正是靠它判断「这个 Worker 中途失联过没有」。
        """
        with self._lock:
            state = self._states.get(call_id)
        if state is None:
            return
        state.stop_event.set()
        thread = state.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, min(self._interval, 1.0)))

    def stop_all(self) -> None:
        """停止全部心跳线程（进程收尾 / 测试清理）。"""
        with self._lock:
            call_ids = list(self._states)
        for call_id in call_ids:
            self.stop(call_id)

    def missed(self, call_id: str) -> int:
        """这个 call 的心跳失败次数（0 表示从未失联）。"""
        with self._lock:
            state = self._states.get(call_id)
        return state.missed if state else 0

    def is_lost(self, call_id: str) -> bool:
        """是否已经失去执行权（续租被拒过）。"""
        with self._lock:
            state = self._states.get(call_id)
        return bool(state and state.lost)

    def stats(self, call_id: str) -> dict[str, Any]:
        """心跳统计 —— 观测 API / 事件 payload 用。"""
        with self._lock:
            state = self._states.get(call_id)
        if state is None:
            return {"call_id": call_id, "beats": 0, "missed": 0, "lost": False}
        return {
            "call_id": call_id,
            "worker_id": state.worker_id,
            "lease_id": state.lease_id,
            "beats": state.beats,
            "missed": state.missed,
            "lost": state.lost,
            "last_error": state.last_error,
        }


class AgentHeartbeat:
    """Agent 心跳：证明 Agent / Workflow 仍然活着（§16 / §17）。

    存成 Hash 而不是 String：心跳天生是「多字段快照」
    （``last_seen`` + ``run_id`` + 递增 ``seq`` + 业务 meta），
    Hash 让排障时 ``HGETALL`` 一眼看全，而不用去解析一段 JSON。
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
        self._ttl = int(config.agent_heartbeat_ttl_seconds)

    # ==================================================================
    def beat(self, agent_id: str, run_id: str, *, meta: dict | None = None) -> None:
        """打一次 Agent 心跳。

        ``meta`` 是给 §17 的决策用的**上下文**，例如
        ``{"tool_name": "run_test", "risk_level": "medium", "execution_mode": "async"}``——
        判活逻辑（在别的模块）据此决定「继续 / 取消 / 转人工」，
        心跳本身只负责如实记录「谁、什么时候、说过什么」。

        ``expire`` 每次刷新：TTL 的语义是「最后一次心跳之后还能活多久」，
        而不是「这个 Agent 总共能活多久」—— 后者会在长任务里把活着的 Agent 判死。
        """
        key = agent_heartbeat_key(agent_id, run_id)
        now = self._clock.time()
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "run_id": run_id,
            "last_seen": now,
            "last_seen_iso": self._clock.now().isoformat(),
            "meta": json.dumps(meta or {}, ensure_ascii=False, default=str),
        }
        self._redis.hset(key, payload)
        # 心跳序列号：单调递增，用于发现「某段时间完全没有心跳」这种静默失联。
        self._redis.hincrby(key, "seq", 1)
        self._redis.expire(key, self._ttl)

    def is_alive(self, agent_id: str, run_id: str) -> bool:
        """Agent 是否仍然活着。

        判据有两条，都要满足：

        1. 键还在（TTL 未过期）—— Redis 的淘汰本身就是第一道判活；
        2. ``now - last_seen <= ttl`` —— 显式比较，防止 TTL 与记录里的时间戳
           用两套时钟算出来的错位（手动时钟下尤其必要）。

        如果为 ``False``，平台不应当直接放弃结果，而是按 §17 分流：
        non-destructive 继续、expensive 取消、dangerous 转人工。
        """
        if not self._redis.hgetall(agent_heartbeat_key(agent_id, run_id)):
            return False
        last = self.last_seen(agent_id, run_id)
        if last is None:
            return False
        return (self._clock.time() - last) <= self._ttl

    def last_seen(self, agent_id: str, run_id: str) -> float | None:
        """最后一次心跳的 epoch 秒；没有记录返回 ``None``。"""
        raw = self._redis.hget(agent_heartbeat_key(agent_id, run_id), "last_seen")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):  # pragma: no cover - 脏数据防御
            return None

    def snapshot(self, agent_id: str, run_id: str) -> dict[str, Any]:
        """读整份心跳快照（``HGETALL``）—— 排障与观测用。"""
        raw = self._redis.hgetall(agent_heartbeat_key(agent_id, run_id))
        if not raw:
            return {}
        out: dict[str, Any] = dict(raw)
        if out.get("meta"):
            try:
                out["meta"] = json.loads(out["meta"])
            except (TypeError, ValueError):  # pragma: no cover
                pass
        return out

    def stop(self, agent_id: str, run_id: str) -> None:
        """主动注销心跳（Agent 正常退出时调用，比等 TTL 更快释放键空间）。"""
        self._redis.delete(agent_heartbeat_key(agent_id, run_id))


__all__ = ["AgentHeartbeat", "WorkerHeartbeat"]
