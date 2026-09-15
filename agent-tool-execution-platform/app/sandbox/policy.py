"""沙箱策略的解析、校验与路径守卫（说明书 §27 / §29）。

这个模块只做**纯计算**，不碰进程、不碰容器 —— 于是它可以被 Tool Registry、
Gateway、Sandbox Manager 任意调用，而不引入任何后端依赖。

三件事：

1. :func:`resolve_policy` —— 把「代码默认值 / YAML / Tool 覆盖 / 单次调用」四层
   合成一个最终生效的 :class:`~app.domain.policy.SandboxPolicy`，并守住 §29 的
   内外层次序。
2. :func:`validate_timeout_layers` —— §29 三层超时的**守门员**（见下）。
3. :func:`ensure_workspace` / :func:`check_path_allowed` —— §24 路径穿越的防线：
   工作区目录名要清洗，写入路径要归一化后再与白名单比对。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

from ..domain.errors import ValidationError
from ..domain.policy import SandboxPolicy

logger = logging.getLogger(__name__)

# §29：沙箱超时要**宽于** Tool 自身超时，留出的这段余量用于
# 「Tool 自己先报 TIMEOUT」→ 平台按 TIMEOUT 分类 → 走 Recovery Policy。
# 余量太薄（或干脆反向）会让沙箱抢在 Tool 前面强杀进程，
# 错误分类从 TIMEOUT 退化成 SANDBOX_ERROR，Recovery 表里这一行查不到，
# 重试语义就跟着变了。
MIN_LAYER_MARGIN_S = 10.0

# 目录名清洗：白名单之外的字符一律替换成 ``_``。
# 用白名单而不是「黑名单掉 / 和 ..」，是因为黑名单永远漏 ——
# Windows 下 ``\``、``:``、保留设备名（CON/NUL）都能造成意外。
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MULTI_DOT = re.compile(r"\.{2,}")


# --------------------------------------------------------------------------- #
# 策略合成
# --------------------------------------------------------------------------- #
def resolve_policy(
    base: SandboxPolicy,
    *,
    timeout_ms: int | None = None,
    overrides: dict | None = None,
) -> SandboxPolicy:
    """合成最终生效的沙箱策略。

    :param base: 基线策略（通常来自 ``config.effective_sandbox(tool_name)``）
    :param timeout_ms: 本次调用的 Tool 超时（毫秒，§21 的 ``timeout: int``）。
        沙箱超时会按 §29 被**抬到它之上**，绝不压到它之下。
    :param overrides: 更外层的临时覆盖（如人工审批时降配、演练时缩配额）。

    ``overrides`` 里的未知字段会被**丢弃并告警**，而不是报错：
    字段名写错（``netwrok: true``）时保留默认值，方向是**更严**而不是更松
    （§27 的默认值全是保守值：断网、有限核数），因此容忍配置漂移是安全的；
    反过来，若因为一个拼写错误就让整个平台起不来，运维会倾向于关掉校验。
    """
    payload = base.model_dump()

    if overrides:
        known = set(SandboxPolicy.model_fields)
        unknown = sorted(set(overrides) - known)
        if unknown:
            logger.warning(
                "沙箱策略覆盖里含未知字段，已忽略（保持默认的保守值）: %s", ", ".join(unknown)
            )
        for key, value in overrides.items():
            if key in known and value is not None:
                payload[key] = value

    policy = SandboxPolicy.model_validate(payload)
    _assert_sane(policy)

    if timeout_ms is not None and timeout_ms > 0:
        # §29：Tool Timeout < Sandbox Timeout。Sandbox 是外层兜底，
        # 所以取 max —— 既保证比 Tool 宽，也不会被 Tool 反向收窄。
        tool_timeout_s = timeout_ms / 1000.0
        floored = tool_timeout_s + MIN_LAYER_MARGIN_S
        if floored > policy.timeout_seconds:
            logger.debug(
                "沙箱超时按 §29 从 %.1fs 抬到 %.1fs（Tool 自身超时 %.1fs + 余量 %.1fs）",
                policy.timeout_seconds, floored, tool_timeout_s, MIN_LAYER_MARGIN_S,
            )
            policy = policy.model_copy(update={"timeout_seconds": floored})

    return policy


def _assert_sane(policy: SandboxPolicy) -> None:
    """拦住明显不成立的资源配置。

    这些值会直接变成 ``setrlimit`` / ``docker --memory`` 的参数，
    写错（负数、0）在后端只会得到一句语焉不详的 OSError，
    不如在合成策略这一步就说清楚是哪个字段错了。
    """
    problems: list[str] = []
    if policy.cpu <= 0:
        problems.append(f"cpu 必须 > 0，实际 {policy.cpu}")
    if policy.memory_mb <= 0:
        problems.append(f"memory_mb 必须 > 0，实际 {policy.memory_mb}")
    if policy.disk_mb <= 0:
        problems.append(f"disk_mb 必须 > 0，实际 {policy.disk_mb}")
    if policy.timeout_seconds <= 0:
        problems.append(f"timeout_seconds 必须 > 0，实际 {policy.timeout_seconds}")
    if policy.max_processes <= 0:
        problems.append(f"max_processes 必须 > 0（否则 fork bomb 无人拦），实际 {policy.max_processes}")
    if problems:
        raise ValidationError(
            "沙箱策略不合法: " + "; ".join(problems),
            detail={"problems": problems},
        )


# --------------------------------------------------------------------------- #
# §29 三层超时校验
# --------------------------------------------------------------------------- #
def validate_timeout_layers(
    *,
    tool_timeout_ms: int,
    sandbox_timeout_s: float,
    lease_ttl_s: int,
) -> list[str]:
    """校验 §29 的三层超时：``Tool Timeout < Sandbox Timeout < Worker Lease Timeout``。

    返回**违规说明列表**，空列表表示合规。刻意返回列表而不是 bool / 抛异常：
    调用方（启动自检、Tool 注册、人工审批前检查）想把这些问题**一次性全报出来**，
    而不是修一个跑一次。

    这层校验存在的理由是反例本身 —— 见下面对 ``lease_ttl_s <= tool_timeout_s``
    那条的说明。
    """
    violations: list[str] = []

    tool_s = tool_timeout_ms / 1000.0

    if tool_timeout_ms <= 0 or sandbox_timeout_s <= 0 or lease_ttl_s <= 0:
        violations.append(
            f"三层超时存在非正值：tool={tool_ms_desc(tool_timeout_ms)} / "
            f"sandbox={sandbox_timeout_s}s / lease={lease_ttl_s}s。"
            "任何一层为 0 或负都等于「没有这一层」，§29 的兜底链条直接断掉。"
        )

    # --- 第一层 < 第二层 ---------------------------------------------------
    if tool_s >= sandbox_timeout_s:
        violations.append(
            f"违反 §29：Tool Timeout ({tool_s:.1f}s) 应当 **小于** Sandbox Timeout "
            f"({sandbox_timeout_s:.1f}s)，实际不小于。"
            "后果：沙箱会抢在 Tool 自己报超时之前强杀进程，"
            "错误类型从 TIMEOUT 退化成 SANDBOX_ERROR —— "
            "Recovery Policy 表里 TIMEOUT 那一行的处置（Retry / Increase timeout）"
            "根本不会被执行，而 Tool 也没有机会做自己的收尾（回滚、写检查点）。"
        )
    elif sandbox_timeout_s - tool_s < MIN_LAYER_MARGIN_S:
        violations.append(
            f"违反 §29：Tool Timeout ({tool_s:.1f}s) 与 Sandbox Timeout "
            f"({sandbox_timeout_s:.1f}s) 之间只差 {sandbox_timeout_s - tool_s:.1f}s，"
            f"不足建议的余量 {MIN_LAYER_MARGIN_S:.0f}s。"
            "后果：Tool 自己超时、上报、落库、写事件这一串收尾动作还没走完，"
            "沙箱的兜底刀已经落下了，最终状态会变成 SANDBOX_ERROR 而不是 TIMEOUT。"
        )

    # --- 第二层 < 第三层 ---------------------------------------------------
    if sandbox_timeout_s >= lease_ttl_s:
        violations.append(
            f"违反 §29：Sandbox Timeout ({sandbox_timeout_s:.1f}s) 应当 **小于** "
            f"Worker Lease Timeout ({lease_ttl_s}s)，实际不小于。"
            "后果：Worker 的租约已经过期、别的 Worker 合法接管了这次执行，"
            "而本 Worker 的沙箱还在跑 —— 同一份副作用被执行两次。"
        )
    elif lease_ttl_s - sandbox_timeout_s < MIN_LAYER_MARGIN_S:
        violations.append(
            f"违反 §29：Sandbox Timeout ({sandbox_timeout_s:.1f}s) 与 Lease TTL "
            f"({lease_ttl_s}s) 之间只差 {lease_ttl_s - sandbox_timeout_s:.1f}s，"
            f"不足建议的余量 {MIN_LAYER_MARGIN_S:.0f}s。"
            "后果：沙箱杀完进程还要回写结果、释放租约，余量不够时续租/落库会失败，"
            "执行状态卡在「不确定」上，只能转人工。"
        )

    # --- 反例守门员：Tool 跑得比租约还久 ------------------------------------
    #
    # 这就是说明书 §29 点名的错误配置「Tool 300s / Lease 30s」。
    # 单独再报一条，因为它的危害和上面几条不同：上面是「错误分类不准」，
    # 这一条是**数据正确性问题** —— 同一份副作用执行两次。
    if lease_ttl_s <= tool_s:
        violations.append(
            f"严重：Tool Timeout ({tool_s:.1f}s) 已经不小于 Worker Lease Timeout "
            f"({lease_ttl_s}s) —— 即说明书 §29 的反例「Tool 300s / Lease 30s」。"
            "危害链条：Tool 还在跑 → 租约到期（Heartbeat 停更）→ Reaper 判死该 Worker "
            "→ 另一个 Worker 接管这次执行并重新跑一遍 Tool → "
            "**同一份副作用（转账、发消息、写库、下单）被执行两次**；"
            "本 Worker 恢复后还会想续租/回写，其结果覆盖掉接管者的成果。"
            "注意幂等键只能救「可幂等」的 Tool：external_side_effect 级别的副作用救不回来。"
            f"修法：把 Lease TTL 抬到 Sandbox Timeout ({sandbox_timeout_s:.1f}s) + 余量之上，"
            "或调小 Tool 的 timeout_ms。"
        )

    return violations


def tool_ms_desc(timeout_ms: int) -> str:
    """把毫秒描述成人话（``300000 -> 300.0s``）。"""
    return f"{timeout_ms / 1000.0:.1f}s"


# --------------------------------------------------------------------------- #
# 工作区
# --------------------------------------------------------------------------- #
def sanitize_call_id(call_id: str) -> str:
    """把 ``call_id`` 清洗成一个**安全的单层目录名**。

    ``call_id`` 来自外部（Agent 提交、HTTP 请求、重放日志），
    直接拿它拼目录就是 §24 的路径穿越：``../../../../etc`` 会写到工作区之外。
    这里不试图「识别穿越」，而是**白名单重建**：只留 ``[A-Za-z0-9._-]``，
    再把连续的点折叠掉 —— 于是没有分隔符、没有 ``..``、也没有 Windows 的
    ``C:`` 盘符，它只可能是一个普通目录名。
    """
    name = _UNSAFE_NAME_CHARS.sub("_", call_id or "")
    name = _MULTI_DOT.sub("_", name).strip(".")
    if not name:
        # 全被清洗掉（例如 call_id 只由特殊字符组成）→ 用原值的摘要兜底。
        # 用摘要而不是固定字符串，是为了让不同的非法 call_id 落到不同目录，
        # 否则两个不同的调用会共享工作区、互相看到对方的中间文件。
        digest = hashlib.sha1((call_id or "").encode("utf-8")).hexdigest()[:8]
        name = f"call_{digest}"
    return name[:128]


def ensure_workspace(root: str, call_id: str) -> str:
    """创建并返回 ``root/call_id`` 的**绝对路径**。

    幂等：目录已存在时直接复用（同一次 call 可能重试后再次 open）。
    """
    root_path = Path(root).expanduser().resolve()
    name = sanitize_call_id(call_id)
    workspace = (root_path / name).resolve()

    # 兜底防线：清洗逻辑如果哪天被改坏，这里必须挡住。
    # 宁可在 open() 时抛错，也不能让 Tool 跑在一个指向工作区之外的目录里。
    if not _is_within(str(workspace), str(root_path)):
        raise ValidationError(
            f"call_id 清洗后仍逃出工作区根目录: {call_id!r} -> {workspace}",
            detail={"root": str(root_path), "call_id": call_id},
        )

    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


# --------------------------------------------------------------------------- #
# 路径白名单
# --------------------------------------------------------------------------- #
def check_path_allowed(
    path: str,
    policy: SandboxPolicy,
    *,
    workspace: str,
) -> tuple[bool, str]:
    """判断 ``path`` 是否允许被触碰，返回 ``(是否允许, 原因)``。

    允许的三种情况：落在 workspace 内、落在 ``writable_paths`` 下、落在
    ``readonly_paths`` 下（后者**可读不可写**，本函数只回答「能不能碰」，
    写权限由调用方结合 :meth:`SandboxPolicy.writable_paths` 判断）。

    先归一化再比对 —— 这是关键顺序。``workspace/../../etc/passwd``
    在字符串前缀比对下会「看起来」在 workspace 内，归一化之后才现出原形。
    相对路径按 workspace 解析（Tool 里写的 ``./out.txt`` 就落在自己的工作区）。
    """
    if not path:
        return False, "路径为空"

    candidate = path
    if not os.path.isabs(candidate):
        candidate = os.path.join(workspace, candidate)

    # realpath 而非仅 abspath：符号链接是绕过「前缀白名单」的经典手法
    # （workspace/link -> /etc，写 workspace/link/passwd 实际写到 /etc/passwd）。
    normalized = os.path.realpath(os.path.abspath(candidate))

    if _is_within(normalized, workspace):
        return True, f"位于 workspace 内: {workspace}"

    for allowed in policy.writable_paths:
        root = _normalize_root(allowed, workspace)
        if root and _is_within(normalized, root):
            return True, f"命中可写白名单: {allowed}"

    for allowed in policy.readonly_paths:
        root = _normalize_root(allowed, workspace)
        if root and _is_within(normalized, root):
            return True, f"命中只读白名单（可读不可写）: {allowed}"

    # 拒绝时把「做了什么归一化」一起说出来：排查 Tool 的路径 bug 时，
    # 光看到「不允许」没法判断是白名单配少了还是路径写歪了。
    return False, (
        f"路径逃出沙箱边界: {path!r} 归一化后为 {normalized}，"
        f"既不在 workspace({workspace}) 内，也不在 writable_paths"
        f"{list(policy.writable_paths)} / readonly_paths{list(policy.readonly_paths)} 之下"
    )


def _normalize_root(root: str, workspace: str) -> str | None:
    """把一条白名单前缀归一化成绝对路径；空串视为「未配置」。"""
    if not root:
        return None
    candidate = root if os.path.isabs(root) else os.path.join(workspace, root)
    return os.path.realpath(os.path.abspath(candidate))


def _is_within(child: str, parent: str) -> bool:
    """``child`` 是否在 ``parent`` 之下（含 parent 自身）。

    用 ``commonpath`` 而不是字符串 ``startswith``：后者会把
    ``/workspace-evil`` 误判成在 ``/workspace`` 之内（少一个分隔符就骗过前缀比对）。
    Windows 上 ``ntpath`` 大小写不敏感，故用 ``normcase`` 对齐。
    """
    child_n = os.path.normcase(os.path.normpath(child))
    parent_n = os.path.normcase(os.path.normpath(parent))
    if child_n == parent_n:
        return True
    try:
        return os.path.commonpath([child_n, parent_n]) == parent_n
    except ValueError:
        # 不同盘符 / 混合绝对相对路径 —— commonpath 直接报 ValueError，那就是不在之内
        return False
