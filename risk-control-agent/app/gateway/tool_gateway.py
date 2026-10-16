"""工具网关（说明书 §3.2）。

**这是本 Demo 里最重要的一个模块。**

它不只是「给 Agent 加一层权限检查」。它是「Agent 的自主性必须受到约束」
这条主张的**可执行形式** —— 而在真实系统里，一条只能靠提示词维持的约束，
等于没有约束。

网关做五件事，缺一不可：

======================== ====================================================
检查                      为什么
======================== ====================================================
① 工具是否存在            挡住模型编造出来的工具名
② 权限                     挡住越权访问数据
③ 高危操作需要审批号       挡住「Agent 自己决定去改生产配置」
④ 参数与实体范围           挡住「借调查之名对全库做任意查询」
⑤ 全量审计                 没有审计的权限系统，事后无法回答「谁改的」
======================== ====================================================

第 ④ 条容易被忽略，但它是**数据安全**问题而不只是权限问题：
一个调查任务被授权查「用户 u123」，不等于它被授权查「任意用户」。
没有范围约束，一个被污染的告警实体就能让 Agent 去遍历整个用户库。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import RiskLevel, ToolDenyReason
from ..domain.models import ToolCall, ToolSpec, ToolVerdict


@dataclass
class AuditRecord:
    seq: int
    tool: str
    allowed: bool
    permission: str
    kind: str
    risk: str
    deny_reason: str | None
    arguments: dict
    reason: str = ""


@dataclass
class GatewayStats:
    allowed: int = 0
    denied: int = 0
    denied_by_reason: dict[str, int] = field(default_factory=dict)
    dangerous_attempts: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.allowed + self.denied


class ToolGateway:
    """所有工具调用的唯一入口。**没有旁路。**"""

    def __init__(
        self,
        specs: dict[str, ToolSpec],
        grants: set[str],
        *,
        approval_grants: dict[str, str] | None = None,
    ) -> None:
        self.specs = specs
        self.grants = set(grants)
        self.approval_grants = dict(approval_grants or {})
        #: 已批准的动作（由人工审批写入，Agent 自己不能写）
        self.approved_actions: dict[str, str] = {}
        self.audit: list[AuditRecord] = []
        self.stats = GatewayStats()

    # ==================================================================
    # 审批（只有人能调用这个方法）
    # ==================================================================
    def grant_approval(self, tool: str, approval_id: str, *, approver: str) -> str:
        """人工审批通过：为某个具体动作签发一次性审批号。

        注意签名方式：**审批的是「这一次调用」，不是一个永久权限**。
        如果把审批做成「给 Agent 加一个权限」，那它拿到之后就可以
        反复调用 —— 而人当初批准的是某一个具体动作。
        """
        spec = self.specs.get(tool)
        if spec is None:
            raise KeyError(f"未知工具：{tool}")
        if tool not in self.approval_grants:
            raise PermissionError(
                f"{tool}（{spec.kind.value}）不在可审批授予的范围内 —— "
                "改风控配置没有「申请一下就放行」的通道"
            )
        self.approved_actions[approval_id] = tool
        self.audit.append(AuditRecord(
            seq=len(self.audit) + 1, tool=tool, allowed=True,
            permission="(approval)", kind=spec.kind.value, risk=spec.risk.value,
            deny_reason=None, arguments={}, reason=f"人工审批 {approver} 签发 {approval_id}",
        ))
        return approval_id

    # ==================================================================
    # 裁决
    # ==================================================================
    def check(
        self,
        call: ToolCall,
        *,
        scope: set[str] | None = None,
        used_budget: float = 0.0,
        cost_limit: float = 0.0,
    ) -> ToolVerdict:
        seq = len(self.audit) + 1
        spec = self.specs.get(call.tool)

        def deny(reason: ToolDenyReason, message: str) -> ToolVerdict:
            self.stats.denied += 1
            self.stats.denied_by_reason[reason.value] = (
                self.stats.denied_by_reason.get(reason.value, 0) + 1
            )
            if spec is not None and spec.side_effect and spec.risk is RiskLevel.HIGH:
                self.stats.dangerous_attempts.append(call.tool)
            self.audit.append(AuditRecord(
                seq=seq, tool=call.tool, allowed=False,
                permission=spec.permission if spec else "-",
                kind=spec.kind.value if spec else "-",
                risk=spec.risk.value if spec else "-",
                deny_reason=reason.value, arguments=call.arguments, reason=message,
            ))
            return ToolVerdict(allowed=False, tool=call.tool, deny_reason=reason,
                               message=message, audit_id=f"audit_{seq:04d}")

        # ---- ① 工具是否存在 ------------------------------------------
        if spec is None:
            return deny(ToolDenyReason.UNKNOWN_TOOL,
                        f"工具清单里没有 {call.tool!r}")

        # ---- ② 权限 ---------------------------------------------------
        granted = set(self.grants)
        approval_id = call.approval_id
        if approval_id:
            approved_tool = self.approved_actions.get(approval_id)
            if approved_tool == call.tool:
                extra = self.approval_grants.get(call.tool)
                if extra:
                    granted.add(extra)
        if spec.permission not in granted:
            return deny(
                ToolDenyReason.NO_PERMISSION,
                f"{call.tool} 需要权限 {spec.permission}，"
                f"当前持有 {sorted(granted)}；"
                + ("这是高危操作，需要人工审批签发 approval_id"
                   if call.tool in self.approval_grants else "该权限不可自动获得"),
            )

        # ---- ③ 高危操作需要审批号 --------------------------------------
        if call.tool in self.approval_grants and not (
            approval_id and self.approved_actions.get(approval_id) == call.tool
        ):
            return deny(
                ToolDenyReason.NEEDS_APPROVAL,
                f"{call.tool} 是 {spec.risk.value} 风险的写操作，"
                "必须携带人工审批签发的 approval_id",
            )

        # ---- ④a 参数校验 ----------------------------------------------
        missing = [key for key in spec.required_args if key not in call.arguments]
        if missing:
            return deny(ToolDenyReason.BAD_ARGUMENTS,
                        f"{call.tool} 缺少必填参数 {missing}")

        # ---- ④b 实体范围 ----------------------------------------------
        if spec.entity_arg and scope is not None:
            target = str(call.arguments.get(spec.entity_arg, ""))
            if target and target not in scope:
                return deny(
                    ToolDenyReason.OUT_OF_SCOPE,
                    f"{call.tool} 试图访问 {target!r}，它不在本次调查的范围内 —— "
                    "调查授权的是「这个实体的关联集合」，不是「任意查询」",
                )

        # ---- ⑤ 预算 ---------------------------------------------------
        if cost_limit and used_budget + spec.cost > cost_limit:
            return deny(ToolDenyReason.BUDGET_EXHAUSTED,
                        f"调用 {call.tool} 后成本将达到 "
                        f"{used_budget + spec.cost:.1f}，超过上限 {cost_limit:.1f}")

        # ---- 通过 -----------------------------------------------------
        self.stats.allowed += 1
        self.audit.append(AuditRecord(
            seq=seq, tool=call.tool, allowed=True, permission=spec.permission,
            kind=spec.kind.value, risk=spec.risk.value, deny_reason=None,
            arguments=call.arguments, reason=call.reason,
        ))
        return ToolVerdict(allowed=True, tool=call.tool, audit_id=f"audit_{seq:04d}")

    # ==================================================================
    def snapshot(self) -> dict:
        return {
            "total": self.stats.total,
            "allowed": self.stats.allowed,
            "denied": self.stats.denied,
            "denied_by_reason": dict(self.stats.denied_by_reason),
            "dangerous_attempts": list(self.stats.dangerous_attempts),
            "granted": sorted(self.grants),
        }

    def audit_tail(self, limit: int = 8) -> list[AuditRecord]:
        return self.audit[-limit:]


def build_scope(world, entity_id: str) -> set[str]:
    """由告警实体推导出的**调查范围**（§3.2 第 ④ 条）。

    规则很简单：**实体自身 + 与它直接相关的实体**（设备、账号）。

    这不是「够不够用」的问题，而是「授权粒度」的问题：
    调查任务被授权查「这个账号和它的直接关联」，
    而不是「这个数据库」。一条被污染的告警不应该让 Agent 能遍历全库。
    """
    scope: set[str] = {entity_id}
    profile = world.users.get(entity_id)
    if profile is None:
        return scope
    for device_id, device in world.devices.items():
        if entity_id in device.accounts:
            scope.add(device_id)
            scope.update(device.accounts)
    for ip, accounts in world.ips.items():
        if entity_id in accounts:
            scope.add(ip)
            scope.update(accounts)
    return scope
