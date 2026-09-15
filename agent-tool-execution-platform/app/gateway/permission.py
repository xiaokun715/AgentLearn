"""权限校验 —— §26 Permission Check 的落点。

对齐《第十二章：可靠工具执行系统设计说明书》：
- §26 Permission Check：``principal + action`` 的判定，**失败直接拒绝，绝不 retry**
- §5 Tool Metadata 的 ``required_permissions``：Tool 自己声明它需要什么
- §41 状态机 ``VALIDATING`` 阶段的最后一道闸（在参数校验之后、入队之前）
- §51 权限 ≠ 免审批：``admin_agent`` 有 ``database.delete`` 权限，
  但 ``database_delete`` 是 HIGH 风险 Tool，仍然要走人工审批

为什么权限失败必须**直接拒绝**（§26 的核心，也是本模块存在的理由）：

1. **重试不会让权限变好**。权限是 principal 的静态属性，不是网络抖动、
   不是资源竞争。重试一万次结果都一样，只是白白烧掉时间与配额。
2. **重试会掩盖真实问题**。一个 Agent 反复重试 ``database.delete``，
   在有告警的团队里看起来像「下游不稳」；实际是「这个 Agent 被配错了」——
   或者更糟，是一个提示词注入正在试探它能做什么。
   把失败**立刻、响亮**地报出来，才能让配置问题在五分钟内被发现。
3. **它是循环检测的上游**。如果权限失败进重试，就会连带触发 §30 的重复执行
   检测与 §32 的循环升级 —— 用「循环熔断」去兜一个「权限配置错误」，
   等于把安全问题伪装成性能问题。

因此 :attr:`PermissionDecision.allowed` 为 ``False`` 时，调用方应当：
``REJECTED``（§41）+ 审计事件 + **不产生 attempt**。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from ..config import AppConfig
from ..domain.models import ToolMetadata
from ..tools.registry import ToolSpec

logger = logging.getLogger(__name__)


DEFAULT_PERMISSIONS: dict[str, list[str]] = {
    # 与 ``configs/tools.yaml`` 的 permissions 段保持一致。
    # 这里只是「配置缺省时的兜底」，YAML 永远是唯一权威 ——
    # 之所以还要兜底：单测/本地跑 ``AppConfig.for_demo()`` 时没有 YAML，
    # 若此时所有 principal 都是空权限，所有 Tool 都会因为权限失败而拒绝执行，
    # 让人误以为「框架坏了」。而兜底权限刻意**不**含 database.delete，
    # 保证「降级」永远不会变成「提权」。
    "agent_01": ["knowledge.read", "test.execute", "code.execute", "file.read"],
    "agent_02": ["knowledge.read", "test.execute"],
    "agent_no_db": ["knowledge.read", "test.execute"],
    "admin_agent": [
        "knowledge.read",
        "test.execute",
        "code.execute",
        "file.read",
        "database.write",
        "database.delete",
    ],
}


@dataclass
class PermissionDecision:
    """一次权限判定的完整结论（§26）。

    :param allowed: 是否放行
    :param principal: 发起调用的身份（Agent / 用户 / 服务账号）
    :param required: Tool 声明的全部 action
    :param granted: 其中**已获得**的部分
    :param missing: 缺失的部分 —— 直接写进错误信息与审批单，方便排障
    :param reason: 人话解释；``allowed=True`` 时为空
    """

    allowed: bool
    principal: str
    required: list[str] = field(default_factory=list)
    granted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        """序列化进 ``execution_event.payload``（§42）—— 权限拒绝必须可审计。"""
        return {
            "allowed": self.allowed,
            "principal": self.principal,
            "required": list(self.required),
            "granted": list(self.granted),
            "missing": list(self.missing),
            "reason": self.reason,
        }

    def __str__(self) -> str:  # pragma: no cover - 展示用
        if self.allowed:
            return f"allowed principal={self.principal} required={self.required}"
        return (
            f"denied principal={self.principal} missing={self.missing} ({self.reason})"
        )


class PermissionManager:
    """§26 权限表 + 判定逻辑。

    权限模型刻意保持**最小**：``principal -> [action]``，支持 ``*`` 通配。
    没有角色继承、没有策略语言 —— 因为这里的目标不是做一套 IAM，
    而是在「Agent 调 Tool」这一条路径上放一道**确定、可审计、可测试**的闸门。

    通配（§26）::

        test.*   -> 匹配 test.execute / test.read，不匹配 testx.execute
        *        -> 全匹配（慎用，只应给平台自身的管理身份）

    判定顺序（:meth:`check_tool`）：Tool 声明的 ``required_permissions``
    **全部**都要满足才算放行 —— 部分满足等于不满足，
    因为漏掉的那一个可能正是 ``database.write`` 与 ``database.delete``
    之间「只读改成了删库」的差别。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        raw = config.permissions or DEFAULT_PERMISSIONS
        if not config.permissions:
            logger.debug("未配置 permissions，使用 PermissionManager 的内置兜底表")
        self._permissions: dict[str, set[str]] = {
            principal: set(actions or []) for principal, actions in raw.items()
        }

    # ------------------------------------------------------------------
    # 授权管理（运行时增删；生产里应当有独立的审计，不允许 Agent 自己调）
    # ------------------------------------------------------------------
    def grant(self, principal: str, *actions: str) -> None:
        """给 principal 增加权限。"""
        if not actions:
            return
        bucket = self._permissions.setdefault(principal, set())
        bucket.update(actions)
        logger.info("grant %s -> %s", principal, ", ".join(actions))

    def revoke(self, principal: str, *actions: str) -> None:
        """回收权限。

        回收是**精确匹配**（不做通配展开）：写 ``revoke("agent_01", "test.*")``
        只会删掉字面量 ``test.*`` 这条授权，而不会去删它覆盖到的每一个 action。
        这样 API 是幂等的、可预测的；要批量回收就显式列出 action 列表 ——
        「删除一个通配符居然改动了五条权限」是排障时最难查的一类意外。
        """
        if not actions:
            return
        bucket = self._permissions.get(principal)
        if not bucket:
            return
        for action in actions:
            bucket.discard(action)
        logger.info("revoke %s -> %s", principal, ", ".join(actions))

    def permissions_of(self, principal: str) -> list[str]:
        """列出 principal 的全部授权（原样返回，含通配模式）。未知身份返回空列表。"""
        return sorted(self._permissions.get(principal, set()))

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def check(self, *, principal: str, action: str, resource: str = "") -> bool:
        """判定 ``principal`` 能否执行 ``action``（可选带 resource 作用域）。

        ``resource`` 用于「同一个 action，但只允许某个子集」的场景，
        例如 ``file.read:/workspace/data/*``。不传 resource 时，
        带作用域的授权**不生效** —— 因为「能读某个目录」不等于「能读所有目录」。
        """
        for pattern in self._permissions.get(principal, ()):  # type: ignore[arg-type]
            if _match(pattern, action, resource):
                return True
        return False

    def check_tool(self, principal: str, spec: ToolSpec) -> PermissionDecision:
        """检查 principal 是否满足某个 Tool 的**全部**权限要求（§26）。

        返回 :class:`PermissionDecision` 而不是 bool：拒绝时必须能回答
        「缺的是哪一条」，否则运维只能靠猜去改配置。
        """
        return self.decide(
            principal=principal,
            tool_name=spec.metadata.name,
            required=list(spec.metadata.required_permissions),
        )

    def decide(
        self, *, principal: str, tool_name: str, required: list[str]
    ) -> PermissionDecision:
        """权限判定的**唯一实现**（``check_tool`` 与只拿到元数据的调用方共用）。

        为什么只留一条实现路径：如果「从 ToolSpec 判」和「从 ToolMetadata 判」
        是两段代码，早晚会分叉 —— 而权限判定分叉的后果是「同一份配置，
        走 A 路径放行、走 B 路径拒绝」，这种不确定性比权限本身更危险。
        """
        if not required:
            # 无权限要求的 Tool（如 calculator）对任何身份开放。
            # 这是**声明**的结果，不是「忘了配」—— 所以不需要额外兜底。
            return PermissionDecision(allowed=True, principal=principal, required=[])

        granted = [
            action for action in required if self.check(principal=principal, action=action)
        ]
        missing = [action for action in required if action not in granted]

        if not missing:
            return PermissionDecision(
                allowed=True,
                principal=principal,
                required=required,
                granted=granted,
            )

        reason = (
            f"principal {principal!r} 缺少 Tool {tool_name!r} 所需的权限 "
            f"{missing}（§26：权限失败直接拒绝，不 retry —— 重试不会让权限变好，"
            "只会浪费时间并掩盖真实的配置/注入问题）"
        )
        logger.warning("权限拒绝: %s", reason)
        return PermissionDecision(
            allowed=False,
            principal=principal,
            required=required,
            granted=granted,
            missing=missing,
            reason=reason,
        )

    # ------------------------------------------------------------------
    def describe(self) -> dict[str, list[str]]:
        """当前权限表快照（排障/审计用）。"""
        return {
            principal: sorted(actions)
            for principal, actions in sorted(self._permissions.items())
        }


def _match(pattern: str, action: str, resource: str) -> bool:
    """单条授权模式是否覆盖 ``(action, resource)``。

    支持两种写法：

    * ``test.*`` / ``*``         —— 纯 action 通配（§26 要求）
    * ``file.read:/workspace/*`` —— action + resource 作用域

    用 ``fnmatchcase`` 而不是自己写正则：通配语义（``*``/``?``/``[seq]``）
    已经有一套被广泛理解的约定，自己发明一套只会让配置变得难懂。
    """
    if ":" in pattern:
        action_pattern, _, resource_pattern = pattern.partition(":")
        if not resource:
            return False  # 带作用域的授权不能当作「无作用域的全量授权」使用
        return fnmatchcase(action, action_pattern) and fnmatchcase(
            resource, resource_pattern
        )
    return fnmatchcase(action, pattern)


def check_metadata_permissions(
    manager: PermissionManager, principal: str, metadata: ToolMetadata
) -> PermissionDecision:
    """不持有 ``ToolSpec`` 时的便捷入口（例如只从 DB 里读回了元数据）。

    直接转发 :meth:`PermissionManager.decide` —— 判定逻辑只有一份，
    避免「从 Spec 判」与「从 Metadata 判」两条路径漂移。
    """
    return manager.decide(
        principal=principal,
        tool_name=metadata.name,
        required=list(metadata.required_permissions),
    )
