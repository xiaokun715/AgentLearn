"""幂等裁决 —— 把「重复提交」翻译成 Agent 能执行的动作（§9~§13）。

职责
----
:class:`IdempotencyManager` 是 §9 幂等流程在**平台侧**的落点。它做三件事：

1. **抢执行权**（``begin``）—— §10 ``SET NX EX``，并同步写下 ``tool_status:{call_id}``。
2. **写终态**（``complete_success`` / ``complete_failure``）—— §8 + §44/§45 的 CAS 迁移。
3. **裁决**（``decide``）—— 说明书 §9~§13 的**四分支**，把「现状」翻译成
   ``WAIT / RETRY / HUMAN / ABORT``。

四分支图（``decide`` 的核心，务必对照 §11 / §12 / §19 / §35 / §56 一起看）::

                        ┌──────────────────────────────┐
                        │  GET idempotency:{key}       │
                        └──────────────┬───────────────┘
                                       │
        ┌──────────────────┬───────────┴────────┬──────────────────┐
        │ Case 1           │ Case 2             │ Case 3           │ Case 4
        │ record is None   │ PROCESSING         │ SUCCESS          │ FAILED
        ▼                  ▼                    ▼                  ▼
   ┌─────────┐   ┌──────────────────┐   ┌────────────┐   ┌────────────────────┐
   │ RETRY   │   │ lease.is_alive?  │   │ WAIT(兜底) │   │ ErrorType -> 查表   │
   │ 可能根本 │   └───┬──────────┬───┘   │ 实际由网关 │   │ error_type          │
   │ 没提交过 │       │ 是       │ 否    │ 直接复用结果│   └───┬────────────┬───┘
   └─────────┘       ▼          ▼       └────────────┘       │            │
                  ┌──────┐  ┌──────────────────────┐      attempt<   attempt>=
                  │ WAIT │  │ crash_safe_to_retry  │      max        max
                  │ §11  │  │ 且 risk != HIGH ?    │       │            │
                  └──────┘  └───┬──────────────┬───┘       ▼            ▼
                            是  │              │ 否     ┌───────┐   ┌───────┐
                                ▼              ▼        │ 查表   │   │ ABORT │
                            ┌───────┐    ┌──────────┐   │§35    │   │ 最后  │
                            │ RETRY │    │ HUMAN    │   └───────┘   │ 一道闸 │
                            │ §56/57│    │ §56 不确 │               └───────┘
                            └───────┘    │ 定 -> 人工│
                                         └──────────┘

Case 3 的分工（容易看错，特别说明）
----------------------------------
``SUCCESS`` 命中时 ``decide`` 返回的是 ``WAIT``，但它的语义**不是**「请等待」，
而是「什么都别做」。真正的处理分工是：

* **Gateway 是第一道**：``if record.status == SUCCESS: 直接取 result_id 复用（§12）``，
  这是正常路径，根本不会走到 ``decide``。
* **``decide`` 是兜底**：万一有调用方漏判（例如恢复流程里只剩一个 ``record``），
  它必须返回一个「不会导致再次执行」的动作。四个动作里 ``WAIT`` 是唯一安全的 ——
  ``RETRY`` 会重复执行，``ABORT`` 会把一件**已经成功**的事报成失败。
  所以这里选择 ``WAIT`` 并让 ``reason`` 说清楚「幂等命中，直接复用，绝不能再次执行」。

为什么是同步（sync）
--------------------
全链路只做同步的 Redis 命令与 SQLite 写入，单线程内不存在协程切换，
``try_claim`` 与随后的 ``tool_status`` 写入之间不会被其它协程插进来 ——
这与真实 Redis 单线程串行执行模型一致。**不要**在这里引入 ``async/await``。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..config import AppConfig
from ..domain.enums import (
    ErrorType,
    IdempotencyLevel,
    IdempotencyStatus,
    RecoveryAction,
    RiskLevel,
)
from ..domain.models import IdempotencyRecord, ToolCall, idempotency_key_material
from ..domain.policy import RecoveryPolicyConfig
from ..infra.clock import Clock
from ..infra.database import Database
from ..infra.redis import RedisSim, encode, tool_status_key
from .state import ClaimResult, IdempotencyStore, WaitDecision

if TYPE_CHECKING:  # 只用于类型标注：避免 idempotency 与 lease 在导入期互相牵扯
    from ..lease.manager import LeaseManager

logger = logging.getLogger(__name__)


class IdempotencyManager:
    """幂等子系统的对外门面。

    :param redis: 协调层（幂等键、``tool_status``）
    :param db: 事实层（审计事件；终态本身由 Gateway 走 ``finalize_*`` 提交）
    :param config: 平台配置（``idempotency_ttl_seconds`` 等）
    """

    def __init__(
        self,
        redis: RedisSim,
        db: Database,
        config: AppConfig,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._redis = redis
        self._db = db
        self._config = config
        self._clock: Clock = clock or redis.clock
        self._store = IdempotencyStore(
            redis,
            clock=self._clock,
            ttl_seconds=config.idempotency_ttl_seconds,
        )

    # ==================================================================
    @property
    def store(self) -> IdempotencyStore:
        """暴露底层 store —— 恢复流程 / Reaper 需要直接操作幂等记录。"""
        return self._store

    def check(self, key: str) -> IdempotencyRecord | None:
        """只读查询：这个幂等键现在是什么状态（``None`` = 从未提交过）。"""
        return self._store.get(key)

    # ==================================================================
    # 开始一次执行（§10）
    # ==================================================================
    def begin(self, call: ToolCall, *, worker_id: str | None = None) -> ClaimResult:
        """抢「这个逻辑操作」的执行权。

        内部两步（顺序不可颠倒）：

        1. ``call.ensure_idempotency_key()`` —— 缺省时按 §7 配方补全键；
           Agent 可以不自己算，但平台必须算，否则「同一逻辑操作」无从识别。
        2. ``store.try_claim(...)`` —— §10 ``SET NX EX``；**拿到才写**
           ``tool_status:{call_id}``（状态快照，供轮询与排障直接读，无需解析幂等键）。

        :return: :class:`~app.idempotency.state.ClaimResult`。
            调用方拿到 ``acquired=False`` 时**不要**继续执行 Tool，
            应当把 ``record`` 交给 :meth:`decide` 或直接按 ``status`` 分流。
        """
        key = call.ensure_idempotency_key()
        result = self._store.try_claim(
            key=key,
            call_id=call.call_id,
            worker_id=worker_id,
            lease_id=None,  # 租约在 Gateway 拿到执行权之后再申请，拿到后回填
            key_material=idempotency_key_material(
                tenant_id=call.tenant_id,
                workflow_run_id=call.graph_run_id,
                logical_step_id=call.logical_step_id,
                tool_name=call.tool_name,
                arguments=call.arguments,
            ),
        )
        if result.acquired:
            self._write_status(
                call.call_id,
                key=key,
                status=IdempotencyStatus.PROCESSING,
                worker_id=worker_id,
            )
        return result

    # ==================================================================
    # 终态迁移（§8 / §44）
    # ==================================================================
    def complete_success(
        self, key: str, *, call_id: str, result_id: str | None
    ) -> None:
        """标记 SUCCESS 并刷新 ``tool_status``。

        迁移被拒绝（``False``）不是异常，而是**必须留痕的异常情况**：
        说明有一个「同键的迟到者」试图覆盖已终态的记录。会被写进
        ``execution_event``，供 §30 重复执行检测与事后复盘取证。
        """
        migrated = self._store.mark_success(key, call_id=call_id, result_id=result_id)
        if migrated:
            self._write_status(
                call_id,
                key=key,
                status=IdempotencyStatus.SUCCESS,
                result_id=result_id,
            )
        else:
            self._db.append_event(
                call_id,
                "idempotency.transition_rejected",
                {
                    "target_status": IdempotencyStatus.SUCCESS.value,
                    "reason": "记录已不是 PROCESSING（迟到的成功回报被拒绝）",
                    "idempotency_key": key,
                },
            )

    def complete_failure(
        self, key: str, *, call_id: str, error: str, error_type: ErrorType
    ) -> None:
        """标记 FAILED 并刷新 ``tool_status``。

        ``error_type`` 落库成字符串（§34）：记录里存字符串而不是枚举，
        是为了后续用 ``ErrorType(record.error_type)`` 反查时**对不上就报错**，
        而不是静默退化成某种默认错误类型。
        """
        migrated = self._store.mark_failed(
            key, call_id=call_id, error=error, error_type=error_type.value
        )
        if migrated:
            self._write_status(
                call_id,
                key=key,
                status=IdempotencyStatus.FAILED,
                error=error,
                error_type=error_type.value,
            )
        else:
            self._db.append_event(
                call_id,
                "idempotency.transition_rejected",
                {
                    "target_status": IdempotencyStatus.FAILED.value,
                    "error_type": error_type.value,
                    "reason": "记录已不是 PROCESSING（迟到的失败回报被拒绝）",
                    "idempotency_key": key,
                },
            )

    # ==================================================================
    # 四分支裁决（§9~§13 / §56）
    # ==================================================================
    def decide(
        self,
        record: IdempotencyRecord | None,
        *,
        lease_manager: "LeaseManager",
        risk_level: RiskLevel,
        idempotency_level: IdempotencyLevel,
        attempt: int = 0,
        max_attempts: int = 3,
        config: RecoveryPolicyConfig | None = None,
        claim_grace_seconds: float | None = None,
    ) -> WaitDecision:
        """把幂等现状裁决成一个恢复动作（四分支，见模块 docstring 的分支图）。

        :param record: :meth:`check` / :meth:`begin` 拿到的现状，``None`` = 键不存在
        :param lease_manager: 用于判断 PROCESSING 的执行者是否还活着（§14/§15）
        :param risk_level: Tool 的风险等级（§51）；HIGH 在任何一个不确定分支都转人工
        :param idempotency_level: Tool 的幂等性等级（§57），决定能否接管重跑
        :param attempt: 已经尝试过几次；``>= max_attempts`` 时统一降级为 ABORT
        :param max_attempts: 重试上限（§36）
        :param config: :class:`~app.domain.policy.RecoveryPolicyConfig`；缺省取平台配置
        :param claim_grace_seconds: **认领宽限期**，见下。``None`` 取平台配置。

        .. note:: 为什么要「认领宽限期」

           §11 的分支图隐含一个前提：**PROCESSING 就一定有一个租约在持有**。
           同步路径下这成立（submit 当场抢租约）。但异步路径不成立 ——
           Gateway 抢到幂等键之后，任务只是躺进了队列，真正抢租约的是稍后来的 Worker。
           这中间有一个「已认领、尚无租约」的窗口，窗口长度 = 排队等待时间。

           如果在这个窗口里来了第二个相同幂等键的请求，按「无租约 -> 判死 -> 可重跑」
           的朴素逻辑走，就会得出「前一个执行者已经死了」的**错误结论**，
           于是放行第二次执行 —— 恰恰是幂等机制要防的事（§71 Test 1）。

           所以这里加一层时间闸门：认领时间还在宽限期内，一律 WAIT。
           宽限期一过，说明确实没人接手（Worker 挂了 / 队列堵了），
           才进入 §56 的接管判断。**这让「等一等」成为默认，"重跑" 成为需要证据的例外。**
        """
        policy = config or self._config.recovery

        # ---------------- Case 1：键不存在 ----------------
        if record is None:
            # §19 最后一段：查不到键 = 很可能**从没成功提交过**（Agent 在提交前就崩了，
            # 或者第一次 SET NX 之后键已过期）。两种情况都指向同一个安全动作：重新提交。
            # 注意这里敢于 RETRY 的依据是「连 PROCESSING 都没留下」——
            # 与 Case 2「留下了 PROCESSING 但状态不明」是完全不同的风险等级。
            return WaitDecision(
                action=RecoveryAction.RETRY,
                reason=(
                    "§19 幂等键不存在：可能之前根本没成功提交 Tool，"
                    "重新提交是安全的（重提交后由 §10 的 SET NX 保证只会有一个执行者）"
                ),
                record=None,
            )

        status = record.status

        # ---------------- Case 2：PROCESSING ----------------
        if status == IdempotencyStatus.PROCESSING:
            # §11：对方正在执行。只要租约还活着，就说明它还在续租，
            # Agent 的正确动作是 WAIT / POLL —— 既不要重提交（会幂等命中），
            # 更不要自己动手执行。
            if lease_manager.is_alive(record.call_id):
                return WaitDecision(
                    action=RecoveryAction.WAIT,
                    reason=(
                        "§11 幂等命中 PROCESSING 且租约仍有效：执行者还活着，"
                        "应当 WAIT / POLL 而不是重新提交"
                    ),
                    record=record,
                )

            # ---- 认领宽限期（异步路径的「已认领、尚无租约」窗口） ----
            #
            # 没有租约**不等于**执行者死了：异步任务刚被 Gateway 认领、还躺在队列里
            # 等 Worker 来接，这段时间本来就没有租约。这里若按「无租约 -> 判死」处理，
            # 就会放行第二次执行，恰好击穿幂等（§71 Test 1）。
            grace = (
                claim_grace_seconds
                if claim_grace_seconds is not None
                else getattr(self._config, "idempotency_claim_grace_seconds", 0.0)
            )
            if grace > 0 and record.started_at is not None and self._clock is not None:
                # 用 max(0, ...) 夹一下：同一秒内读到的 started_at 可能因为
                # 浮点/时钟回拨而略大于 now，直接格式化会打出 "-0.0s" 这种噪声。
                age = max(0.0, self._clock.time() - record.started_at.timestamp())
                if age < grace:
                    return WaitDecision(
                        action=RecoveryAction.WAIT,
                        reason=(
                            f"幂等命中 PROCESSING 且认领仅 {age:.1f}s（宽限期 {grace:.0f}s）："
                            "任务可能还在排队等待 Worker 接手，此时尚无租约属正常现象，"
                            "应 WAIT 而不是判定执行者死亡"
                        ),
                        record=record,
                    )

            # 租约已过期（或根本没有租约）——**这是整套系统最微妙的一刻**（§56）：
            # 「租约过期」只证明"没人在续租"，不证明"没在执行"。
            # Worker A 可能只是断网，Tool 还在跑，副作用已经发出去了。
            # 所以能不能接管，取决于副作用是否可重复（§57 IdempotencyLevel）。
            if idempotency_level.crash_safe_to_retry:
                if risk_level == RiskLevel.HIGH:
                    # 可重跑的 Tool 也不该自动接管高风险操作：宁可慢一步，不可错一步。
                    return WaitDecision(
                        action=policy.uncertain_high_risk_action,
                        reason=(
                            "§56 租约已过期且执行状态不确定 + 高风险（§51）："
                            "不做自动接管，转人工确认（RECOVERY_REQUIRED）"
                        ),
                        record=record,
                    )
                return WaitDecision(
                    action=policy.uncertain_low_risk_action,
                    reason=(
                        f"§56/§57 租约已过期，但 Tool 的幂等性等级为 "
                        f"{idempotency_level.value}（crash_safe_to_retry=True）且风险为 "
                        f"{risk_level.value}：允许新 Worker 接管重跑"
                    ),
                    record=record,
                )

            # AT_LEAST_ONCE / NON_IDEMPOTENT：重跑可能产生第二次真实副作用
            # （再发一条消息、再扣一次款）。§57 的态度是「先查询状态，再决定」，
            # 平台侧一律先把人拉进来 —— 这条分支**刻意不读 YAML**：
            # 安全阀不能被配置放松成 RETRY。
            return WaitDecision(
                action=RecoveryAction.HUMAN,
                reason=(
                    f"§56/§57 租约已过期且执行状态不确定，Tool 的幂等性等级为 "
                    f"{idempotency_level.value}（不可安全重跑）："
                    "转人工 / 走外部事务号对账，绝不自动重执行"
                ),
                record=record,
            )

        # ---------------- Case 3：SUCCESS ----------------
        if status == IdempotencyStatus.SUCCESS:
            # 正常路径下 Gateway 会先看 status == SUCCESS 直接取 result_id 复用（§12），
            # 压根不会调到这里。这里返回 WAIT 是**兜底**：它表示「不需要任何动作」，
            # 因为另外三个动作都会造成伤害（RETRY 重复执行 / ABORT 误报失败）。
            return WaitDecision(
                action=RecoveryAction.WAIT,
                reason=(
                    "§12 幂等命中 SUCCESS：直接复用已持久化的结果，"
                    "**绝不能再次执行**（调用方应先判 status == SUCCESS 走取结果分支，"
                    "本分支仅作兜底）"
                ),
                record=record,
            )

        # ---------------- Case 4：FAILED ----------------
        # §34/§35：FAILED 不是终点，先还原错误分类，再查恢复策略表。
        # 还原失败说明记录被写脏了 —— 归到 INTERNAL_ERROR（默认 ABORT）而不是猜一个。
        error_type = self._resolve_error_type(record)

        if attempt >= max_attempts:
            # **防无限重试的最后一道闸**：即使策略表说 RETRY，也不能再放了。
            # 少了这道闸，一个「每次都超时」的 Tool 会无限重试，
            # 把下游打满、把幂等键的 TTL 反复续命，最终变成看不见的资源泄漏。
            return WaitDecision(
                action=RecoveryAction.ABORT,
                reason=(
                    f"§36 重试次数已达上限（attempt={attempt} >= max_attempts={max_attempts}）："
                    f"策略表原本给出 {policy.action_for(error_type).value}，"
                    "此处降级为 ABORT 以终止无限重试"
                ),
                record=record,
            )

        action = policy.action_for(error_type)
        return WaitDecision(
            action=action,
            reason=(
                f"§35 错误分类 {error_type.value} -> 恢复动作 {action.value}"
                f"（attempt={attempt}/{max_attempts}）"
            ),
            record=record,
        )

    # ==================================================================
    # 内部工具
    # ==================================================================
    @staticmethod
    def _resolve_error_type(record: IdempotencyRecord) -> ErrorType:
        """从记录里还原 :class:`~app.domain.enums.ErrorType`。

        记录里是字符串，这里显式枚举化：拼写对不上就说明写入方与读取方不一致，
        属于平台缺陷，归到 ``INTERNAL_ERROR``（策略表里是 ABORT）比默默按
        BUSINESS_ERROR 处理安全 —— 后者容易被配置成 RETRY。
        """
        raw = record.error_type
        if not raw:
            return ErrorType.INTERNAL_ERROR
        try:
            return ErrorType(raw)
        except ValueError:
            logger.warning("unknown error_type in idempotency record: %r", raw)
            return ErrorType.INTERNAL_ERROR

    def _write_status(
        self,
        call_id: str,
        *,
        key: str,
        status: IdempotencyStatus,
        worker_id: str | None = None,
        result_id: str | None = None,
        error: str | None = None,
        error_type: str | None = None,
    ) -> None:
        """写 ``tool_status:{call_id}`` 快照。

        为什么要在幂等键之外再存一份：Agent 轮询的是 **call_id**（它手里只有这个），
        而幂等键是 ``idem_xxxx`` 形式的哈希。让 Agent 为了查状态去重算哈希
        既啰嗦又容易算错（少一个字段就换了另一个键、查到的是别人的状态）。
        这份快照 TTL 与幂等键一致，两者同生共死。
        """
        payload = {
            "call_id": call_id,
            "status": status.value,
            "idempotency_key": key,
            "worker_id": worker_id,
            "result_id": result_id,
            "error": error,
            "error_type": error_type,
            "updated_at": self._clock.now().isoformat(),
        }
        self._redis.set(
            tool_status_key(call_id), encode(payload), ex=self._config.idempotency_ttl_seconds
        )


__all__ = ["IdempotencyManager"]
