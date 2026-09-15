"""异步执行器 —— 长任务的队列路径（说明书 §4.2 / §62）。

同步路径让 Agent 一直等着；对 1~5 分钟的测试任务，这是不可接受的：
Agent 线程被占住、HTTP 连接超时、进程重启就全丢。所以异步路径的做法是：

::

    Agent
      │
      ▼
    Tool Gateway
      │
      ▼
    Async Scheduler  ──► Create Job ──► Return job_id
      │                                      │
      ▼                                      ▼
    Redis Stream(JOB_STREAM)           Agent Checkpoint
      │
      ▼
    Worker ──► Acquire Lease ──► Sandbox ──► Tool ──► Result

**关键点：返回 job_id 之后 Agent 不再持有这次执行。** 它把 ``call_id`` /
``idempotency_key`` 写进 Checkpoint（§18）然后去做别的事（或者干脆崩掉）。
Tool 完成时通过 §50 的 callback 事件把 Graph 唤醒。

队列用 **Redis Streams 的消费者组**（§69），而不是简单的 LPUSH/BRPOP，原因是 PEL：
消息投递出去但没 ACK 时留在 Pending Entries List 里，Worker 崩溃后能被别的 Worker
用 ``XAUTOCLAIM`` 捞回来重做 —— 这是 §56「Worker Crash 恢复」的**数据基础**。
简单的 list 队列做不到这一点（消息一弹出就没了，Worker 死了任务就永久丢失）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from ..config import AppConfig
from ..domain.enums import ExecutionMode, RiskLevel
from ..domain.models import ToolCall
from ..infra.clock import Clock, SystemClock
from ..infra.redis import JOB_GROUP, JOB_STREAM, RedisSim

logger = logging.getLogger(__name__)

DELAYED_ZSET = "tool_jobs:delayed"
"""延迟队列（§36 退避重试的落点）。

重试不原地 sleep —— 那会把 Worker 占住。改成「把任务放到 delayed zset，
到点了再由任意 Worker 提升回主队列」。这样退避成本由队列承担，不占用执行资源。
"""


@dataclass
class JobPayload:
    """队列消息体。刻意只放**指针**，不放结果。"""

    call_id: str
    tool_name: str
    tool_version: str = "1.0"
    arguments: dict = None            # type: ignore[assignment]
    idempotency_key: str = ""
    tenant_id: str = "tenantA"
    agent_id: str = ""
    run_id: str = ""
    session_id: str = ""
    logical_step_id: str = "step_1"
    attempt: int = 0
    risk_level: str = RiskLevel.LOW.value
    timeout_ms: int = 0

    def __post_init__(self) -> None:
        if self.arguments is None:
            self.arguments = {}

    def to_call(self) -> ToolCall:
        return ToolCall(
            call_id=self.call_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            graph_run_id=self.run_id,
            tenant_id=self.tenant_id,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            arguments=self.arguments,
            idempotency_key=self.idempotency_key,
            logical_step_id=self.logical_step_id,
            attempt=self.attempt,
        )

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "JobPayload":
        data = json.loads(raw)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_call(cls, call: ToolCall, *, risk_level: str = "low", timeout_ms: int = 0) -> "JobPayload":
        return cls(
            call_id=call.call_id,
            tool_name=call.tool_name,
            tool_version=call.tool_version,
            arguments=dict(call.arguments),
            idempotency_key=call.ensure_idempotency_key(),
            tenant_id=call.tenant_id,
            agent_id=call.agent_id,
            run_id=call.graph_run_id,
            session_id=call.session_id,
            logical_step_id=call.logical_step_id,
            attempt=call.attempt,
            risk_level=risk_level,
            timeout_ms=timeout_ms,
        )


class AsyncExecutor:
    """异步调度器：把任务放进队列，立刻返回 job_id。"""

    def __init__(
        self,
        redis: RedisSim,
        config: AppConfig,
        *,
        clock: Optional[Clock] = None,
        consumer_group: str = JOB_GROUP,
        stream_key: str = JOB_STREAM,
    ) -> None:
        self.redis = redis
        self.config = config
        self.clock = clock or SystemClock()
        self.group = consumer_group
        self.stream = stream_key
        self._ensure_group()

    def _ensure_group(self) -> None:
        """建消费者组（幂等）。组已存在时 Redis 返回 False，属正常路径。"""
        try:
            self.redis.xgroup_create(self.stream, self.group, mkstream=True)
        except Exception:  # noqa: BLE001 - 组已存在不算错误
            logger.debug("consumer group %s already exists", self.group)

    # ==================================================================
    def submit(
        self,
        call: ToolCall,
        *,
        risk_level: str = RiskLevel.LOW.value,
        timeout_ms: int = 0,
    ) -> str:
        """入队并返回 ``job_id``（即 Stream 消息 ID）。

        注意这里**不做幂等判断** —— 幂等是 Gateway 的职责，且必须在入队**之前**完成。
        否则会出现「两次提交都入队成功、Worker 各跑一次」的窗口。
        """
        payload = JobPayload.from_call(call, risk_level=risk_level, timeout_ms=timeout_ms)
        job_id = self.redis.xadd(self.stream, {"payload": payload.to_json()})
        logger.debug("enqueued job %s (call %s)", job_id, call.call_id)
        return job_id

    def retry_later(self, payload: JobPayload, *, delay_ms: int) -> None:
        """把一次待重试的任务放进延迟队列（§36 退避，不占 Worker）。"""
        ready_at = self.clock.time() + max(0, delay_ms) / 1000.0
        payload.attempt += 1
        self.redis.zadd(DELAYED_ZSET, {payload.to_json(): ready_at})

    def promote_delayed(self, *, limit: int = 50) -> int:
        """把到期的延迟任务提升回主队列。返回提升条数。

        任何 Worker 都能做这件事 —— 不需要一个专门的调度器进程，
        因此不存在「调度器单点挂掉导致所有重试卡死」的问题。
        """
        now = self.clock.time()
        due = self.redis.zrangebyscore(DELAYED_ZSET, 0, now)[:limit]
        promoted = 0
        for raw in due:
            if self.redis.zrem(DELAYED_ZSET, raw):
                self.redis.xadd(self.stream, {"payload": raw})
                promoted += 1
        if promoted:
            logger.debug("promoted %d delayed jobs", promoted)
        return promoted

    # ==================================================================
    def queue_depth(self) -> int:
        """**待消费**任务数 —— 「还没被任何 Worker 取走」的任务。

        注意不是 ``XLEN``：Stream 里已消费的条目不会消失（不像 List 弹出即删），
        所以 ``XLEN`` 是个只增不减的累计值。真正的待办量是
        「ID 大于消费者组 ``last_delivered_id`` 的条目数」。
        """
        for group in self.redis.xinfo_groups(self.stream):
            if group["name"] == self.group:
                return int(group["unconsumed"])
        return self.redis.xlen(self.stream)

    def total_entries(self) -> int:
        """Stream 累计条目数（``XLEN``）—— 只用于排障，别当队列长度看。"""
        return self.redis.xlen(self.stream)

    def delayed_depth(self) -> int:
        """延迟队列里等待退避到期的任务数（§36）。"""
        return self.redis.zcard(DELAYED_ZSET)

    def pending_count(self, *, consumer: Optional[str] = None) -> int:
        """PEL 长度 —— 「已投递未 ACK」的任务数，即**可能卡住的任务**。"""
        pending = self.redis.xpending(self.stream, self.group)
        if consumer is None:
            return len(pending)
        return sum(1 for _, owner, _ in pending if owner == consumer)

    def stats(self) -> dict[str, Any]:
        """队列全景，供观测与演示收尾打印。"""
        return {
            "stream": self.stream,
            "group": self.group,
            "queue_depth": self.queue_depth(),
            "delayed_depth": self.delayed_depth(),
            "pending": self.pending_count(),
            "total_entries": self.total_entries(),
        }


def default_risk_for(config: AppConfig, tool_name: str) -> str:
    """从配置里取 Tool 的默认风险等级（入队时快照，避免 Worker 侧再查一次）。"""
    override = config.tool_override(tool_name)
    if override is not None and override.risk:
        return override.risk
    return RiskLevel.LOW.value


__all__ = [
    "AsyncExecutor",
    "JobPayload",
    "DELAYED_ZSET",
    "default_risk_for",
    "ExecutionMode",
]
