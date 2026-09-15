"""参数注入防护 —— §24 Injection Guard 的实现。

对齐《第十二章：可靠工具执行系统设计说明书》：
- §24 参数注入防护（Path Traversal / Command Injection / SQL Injection / SSRF）
- §25 ``ParamRule.injection_check``：逐参数开关
- §34 ``InjectionDetected`` 归入 ``VALIDATION_ERROR`` 且**绝不重试**
- §63 安全测试矩阵里的注入用例

设计要点（为什么这样做）：

1. **只在参数进入平台时检查，而不是交给 Tool 自己防**。
   Tool 一旦拿到 ``"../../../etc/passwd"`` 就已经晚了 —— 它可能已经在沙箱里发起
   了这次读文件。参数是**唯一**的共同咽喉，所以把检查放在 Tool Gateway 的
   参数校验之后、执行之前（§21 的流水线里）。
2. **检测结果带 category / severity / evidence，而不只是一个布尔值**。
   安全事件必须能审计到「命中的是哪段原文」，否则事后只能看到一句「被拦了」，
   无从判断是误报还是真攻击。
3. **路径类必须归一化后再判断**。
   ``"..%2f..%2fetc/passwd"``、``"....//....//etc/passwd"``、``"C:\\Users\\..\\..\\Windows"``
   在字符串层面都**不含**裸的 ``"../"``，只做 ``"../" in value`` 必然漏。所以这里
   先反复 URL 解码，再 ``os.path.normpath`` 归一化，最后按**路径分段**判断。
4. **不做「代码审计」**。
   ``execute_python`` 的 ``code`` 参数里出现 ``import os`` 是**正常**的 —— 那段代码
   本来就该在沙箱里跑。这里只拦「参数被拿去拼命令/拼 SQL/拼路径」这一类注入，
   执行安全交给沙箱（§27/§29）。把两者混在一起只会制造大量误报，最终导致
   检测被关掉 —— 那才是真正的安全事故。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from ..domain.errors import InjectionDetected
from ..domain.policy import ParamRule

# ----------------------------------------------------------------------
# 常量：分类与分级
# ----------------------------------------------------------------------
CATEGORY_PATH_TRAVERSAL = "path_traversal"
CATEGORY_COMMAND_INJECTION = "command_injection"
CATEGORY_SQL_INJECTION = "sql_injection"
CATEGORY_SSRF = "ssrf"
CATEGORY_PROMPT_INJECTION = "prompt_injection"

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"

EVIDENCE_MAX_LEN = 80
"""``evidence`` 只留命中片段的前 80 字符 —— 审计要看的是「原文长什么样」，
不是把整段 20KB 的代码再抄一遍进事件表。"""

_UNQUOTE_ROUNDS = 3
"""反复解码轮数：攻击者常用「双重编码」绕过一次 unquote。"""

_MAX_WALK_DEPTH = 6
"""嵌套结构递归深度上限，防止病态输入把检测器拖死。"""

# 路径类参数：这些根目录之外出现的**绝对路径**视为「逃逸」
_SAFE_PATH_ROOTS: tuple[str, ...] = ("/workspace", "/tmp", "/var/tmp")

# 字段名带这些词 => 语义上是「要执行的命令」，按最严标准检查
_COMMANDISH_TOKENS: tuple[str, ...] = (
    "command",
    "cmd",
    "shell",
    "argv",
    "bash",
    "script",
    "exec",
)

# 字段名带这些词 => 语义上是「要访问的地址」，裸 IP/主机名也要查 SSRF
_HOSTISH_TOKENS: tuple[str, ...] = (
    "host",
    "url",
    "uri",
    "endpoint",
    "webhook",
    "callback",
    "proxy",
    "address",
    "server",
    "domain",
    "target",
)

# 字段名带这些词 => 语义上「会被拼进 SQL」，才做 SQL 注入检查。
#
# 为什么要按字段语义圈定范围，而不是「凡是字符串都查 SQL」：
# SQL 注入的特征串（`sleep(`、`--`、`; DROP`、`UNION SELECT`）在**别的语言里
# 是合法语法**。`execute_python.code = "import time; time.sleep(120)"` 会被
# 「时间盲注函数」规则命中 —— 这是一段完全正当的 Python，却过不了 Gateway。
#
# 误报和漏报一样有害：误报会逼着使用者关掉整条检测（`injection_check: false`），
# 于是真正的注入也一起漏了。所以这里和 _COMMANDISH_TOKENS / _HOSTISH_TOKENS
# 用同一条思路 —— **只在该语义的字段上启用该语义的规则**。
#
# 注意刻意**不含** `query`：`search_knowledge.query` 是自然语言检索词，
# 不是 SQL；把它划进来会让「select the best doc」这类正常提问被拦。
_SQLISH_TOKENS: tuple[str, ...] = (
    "sql",
    "where",
    "stmt",
    "statement",
    "condition",
    "clause",
    "filter",
)

_DANGEROUS_SCHEMES: dict[str, str] = {
    "file": "file:// 可读本地任意文件（SSRF 本地文件读取）",
    "gopher": "gopher:// 可构造任意 TCP 报文（经典 SSRF 打内网服务）",
    "dict": "dict:// 可探测内网端口与服务指纹",
    "ftp": "ftp:// 可能被用于内网穿透与文件外带",
    "ldap": "ldap:// 可用于内网目录服务探测",
    "jar": "jar:// 可读取归档内文件",
    "netdoc": "netdoc:// 可读取本地文件（Java 特有）",
}

# ----------------------------------------------------------------------
# 预编译正则
# ----------------------------------------------------------------------
_RE_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{1,15})://([^\s'\"<>()\[\]]+)")

_RE_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

_RE_HOST_LITERAL = re.compile(
    r"(?i)\b(localhost|metadata\.google\.internal|metadata\.azure\.com"
    r"|169\.254\.169\.254|0\.0\.0\.0|\[::1\]|::1)\b"
)

_RE_WINDOWS_DRIVE = re.compile(r"(?i)^[a-z]:[\\/]")
_RE_UNC = re.compile(r"^[\\/]{2}[^\\/]")
_RE_NULL_BYTE = re.compile(r"(?:\x00|%00)")
# 注意 ``[\\/]+`` 而不是 ``[\\/]``：``....//....//`` 里的**双**斜杠是这类绕过的
# 常见写法（有的解析器会把 ``//`` 折叠成 ``/``），单斜杠版本会漏掉它。
_RE_DOTDOT_BYPASS = re.compile(r"(?:\.\.+[\\/]+){2,}")
_RE_DEVICE_PATH = re.compile(r"(?i)^\\\\[.?]\\")

# 命令注入：强特征（几乎不可能是正常业务文本）
_RE_CMD_STRONG: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\$\("), "$( ) 命令替换"),
    (re.compile(r"\$\{"), "${ } 变量展开（可用于拼接命令）"),
    (re.compile(r"`"), "反引号命令替换"),
    (re.compile(r"\|\|"), "|| 串联命令"),
    (re.compile(r"&&"), "&& 串联命令"),
)
# 命令注入：弱特征（重定向、管道、连接符）—— 只在 command 语义字段里算命中，
# 否则 ``expression: "1 > 0"`` 这种正常比较表达式会被误报。
_RE_CMD_WEAK: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\|"), "管道符 |"),
    (re.compile(r">"), "输出重定向 >"),
    (re.compile(r"<"), "输入重定向 <"),
    (re.compile(r"&"), "后台执行 &"),
)
# 分号/换行只有在 command 语义字段才算强证据：
# Python 源码里换行遍地都是，若对 ``code`` 字段生效会 100% 误报。
_RE_CMD_SHELL_LINE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r";\s*\S"), "; 分号串联命令"),
    (re.compile(r"[\r\n]\s*\S"), "换行注入新命令"),
    (re.compile(r"%0[ad]"), "URL 编码的换行（HTTP 头/命令注入）"),
)

# SQL 注入
_RE_SQL_STRONG: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bunion\b[\s/*]+select\b"), "UNION SELECT 联合查询注入"),
    (
        re.compile(
            r"(?i)\bor\s+(?:\d+|'[^']*'|\"[^\"]*\")\s*=\s*(?:\d+|'[^']*'|\"[^\"]*\")"
        ),
        "OR 1=1 式恒真条件",
    ),
    (
        re.compile(
            r"(?i);\s*(?:drop|delete|update|insert|truncate|alter|grant|shutdown|create)\b"
        ),
        "堆叠查询（; + DDL/DML）",
    ),
    (re.compile(r"(?i)\bxp_cmdshell\b"), "xp_cmdshell 执行系统命令"),
    (re.compile(r"(?i)\binformation_schema\b"), "探测 information_schema 元数据"),
    (
        re.compile(r"(?i)\b(?:sleep|benchmark|pg_sleep)\s*\("),
        "时间盲注函数",
    ),
    (re.compile(r"(?i)\bwaitfor\s+delay\b"), "SQL Server 时间盲注"),
    (
        re.compile(r"(?i)\b(?:load_file|into\s+outfile|into\s+dumpfile)\b"),
        "文件读写型注入",
    ),
)
_RE_SQL_MEDIUM: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"--\s*\S*$"), "SQL 行注释 --（截断后半句）"),
    (re.compile(r"/\*.*?\*/", re.S), "SQL 块注释 /* */"),
    (re.compile(r"(?i)\bchar\s*\(\s*\d+\s*\)"), "char() 编码绕过"),
)

# Prompt Injection：控制面 token 一旦出现就是明确的越狱尝试
_RE_PROMPT_STRONG: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?"
            r"(?:the\s+|your\s+)?(?:previous|prior|above|earlier)\s+"
            r"(?:instruction|prompt|rule|message)s?"
        ),
        "覆盖系统指令（ignore previous instructions）",
    ),
    (
        re.compile(r"(?i)<\|(?:im_start|im_end|system|endoftext|start_header_id)\|>"),
        "伪造对话控制 token",
    ),
    (re.compile(r"(?i)\[\s*/?\s*INST\s*\]"), "伪造 [INST] 指令标记"),
    (re.compile(r"(?im)^\s*system\s*:"), "伪造 system: 角色行"),
    (re.compile(r"(?i)<\s*/?\s*system\s*>"), "伪造 <system> 标签"),
    (
        re.compile(r"忽略(?:上面|以上|之前|前面|前面所有)(?:的)?(?:所有)?(?:指令|要求|规则|提示|设定)"),
        "覆盖系统指令（中文变体）",
    ),
)
_RE_PROMPT_MEDIUM: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\byou\s+are\s+now\b"), "角色重置句式"),
    (re.compile(r"(?i)\b(?:jailbreak|dan\s+mode|developer\s+mode|do\s+anything\s+now)\b"), "越狱模式关键词"),
    (
        re.compile(r"(?i)\b(?:reveal|print|show|repeat)\s+(?:me\s+)?(?:your\s+)?(?:the\s+)?(?:system\s+)?(?:prompt|instructions?)\b"),
        "套取系统提示词",
    ),
    (re.compile(r"(?i)\bpretend\s+(?:to\s+be|you\s+are)\b"), "角色扮演绕过"),
    (re.compile(r"你现在(?:是|扮演)|假装你是"), "角色扮演绕过（中文变体）"),
)


# ======================================================================
# 检测结果
# ======================================================================
@dataclass
class InjectionFinding:
    """一条注入命中记录（§24）。

    :param field: 命中的参数名（嵌套结构里带下标，如 ``test_cases[0]``）
    :param category: ``path_traversal`` / ``command_injection`` / ``sql_injection``
        / ``ssrf`` / ``prompt_injection``
    :param severity: ``high``（明确恶意）/ ``medium``（可疑，可能误报）
    :param evidence: 命中的**原文片段**（截断到 80 字符），用于审计与误报复盘
    :param detail: 人话解释「这为什么危险」
    """

    field: str
    category: str
    severity: str
    evidence: str
    detail: str

    @property
    def is_high(self) -> bool:
        return self.severity == SEVERITY_HIGH

    def to_dict(self) -> dict[str, str]:
        """序列化进 ``execution_event.payload``（§42）—— 安全事件必须可审计。"""
        return {
            "field": self.field,
            "category": self.category,
            "severity": self.severity,
            "evidence": self.evidence,
            "detail": self.detail,
        }

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return (
            f"[{self.category}/{self.severity}] {self.field}: {self.detail} "
            f"| evidence={self.evidence!r}"
        )


# ======================================================================
# 工具函数
# ======================================================================
def _truncate_evidence(text: str) -> str:
    """截取命中原文作为证据；换行/回车压成可见转义，避免污染日志。"""
    head = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(head) > EVIDENCE_MAX_LEN:
        return head[: EVIDENCE_MAX_LEN - 3] + "..."
    return head


def _snippet(text: str, match: re.Match[str], *, pad: int = 16) -> str:
    """取命中位置前后一小段上下文 —— 只留 ``"DROP"`` 三个字母对复盘没帮助。"""
    start = max(0, match.start() - pad)
    end = min(len(text), match.end() + pad)
    return _truncate_evidence(text[start:end])


def _deep_unquote(text: str) -> str:
    """反复 URL 解码，直到不再变化。

    为什么不是解一次就够了：``%252e%252e%252f`` 解一次得到 ``%2e%2e%2f``，
    仍然是安全的字面量；真正危险的是解到 ``"../"`` 的那一刻。
    """
    out = text
    for _ in range(_UNQUOTE_ROUNDS):
        nxt = unquote(out)
        if nxt == out:
            break
        out = nxt
    return out


def _is_commandish(field: str) -> bool:
    low = field.lower()
    return any(token in low for token in _COMMANDISH_TOKENS)


def _is_hostish(field: str) -> bool:
    low = field.lower()
    return any(token in low for token in _HOSTISH_TOKENS)


def _is_sqlish(field: str) -> bool:
    low = field.lower()
    return any(token in low for token in _SQLISH_TOKENS)


def _to_posix(path: str) -> str:
    """统一成 POSIX 风格并归一化。

    Windows 上 ``os.path.normpath("C:\\\\Windows\\\\")`` 会返回反斜杠形式，
    而策略里的 ``denied_prefix`` 可能写成 ``/etc/`` 这种 POSIX 形式 ——
    两边必须先归一化到同一坐标系，比较才有意义（Windows 路径也要能匹配）。
    """
    return os.path.normpath(path.replace("\\", "/")).replace("\\", "/")


def _octets(ip: str) -> Optional[tuple[int, int, int, int]]:
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    try:
        values = tuple(int(p) for p in parts)
    except ValueError:
        return None
    if any(v < 0 or v > 255 for v in values):
        return None
    return values  # type: ignore[return-value]


def _is_internal_ip(ip: str) -> Optional[str]:
    """判定内网/回环/链路本地地址，返回人类可读原因（不是内网则 None）。"""
    octets = _octets(ip)
    if octets is None:
        return None
    a, b, _c, _d = octets
    if ip == "169.254.169.254":
        return "云元数据服务地址（可直接窃取实例凭证）"
    if a == 127:
        return "回环地址（可访问宿主本机服务）"
    if a == 10:
        return "内网地址 10.0.0.0/8"
    if a == 172 and 16 <= b <= 31:
        return "内网地址 172.16.0.0/12"
    if a == 192 and b == 168:
        return "内网地址 192.168.0.0/16"
    if a == 169 and b == 254:
        return "链路本地地址（常见云元数据段）"
    if a == 100 and 64 <= b <= 127:
        return "运营商级 NAT 地址 100.64.0.0/10"
    if a == 0:
        return "0.0.0.0/8（在部分系统上等价于本机）"
    return None


def _decode_obfuscated_ip(token: str) -> Optional[str]:
    """识别 ``2130706433`` / ``0x7f000001`` / ``017700000001`` 这类「整数 IP」。

    很多 SSRF 过滤器只认点分十进制，攻击者于是把 127.0.0.1 写成十进制整数 ——
    HTTP 客户端照样会解析成回环地址。
    """
    try:
        if token.lower().startswith("0x"):
            value = int(token, 16)
        elif len(token) > 1 and token.startswith("0") and token.isdigit():
            value = int(token, 8)
        elif token.isdigit():
            value = int(token)
        else:
            return None
    except ValueError:
        return None
    if value < 0 or value > 0xFFFFFFFF:
        return None
    ip = ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))
    return ip


# ======================================================================
# 检测器
# ======================================================================
class InjectionDetector:
    """§24 参数注入检测器（同步、无状态、可重复调用）。

    用法::

        detector = InjectionDetector()
        findings = detector.scan_arguments(call.arguments, rules=policy.rules)
        detector.assert_clean(call.arguments, rules=policy.rules)  # 命中就抛

    ``rules`` 只用来**关闭**某些字段的检查（``injection_check: false``）：
    没有规则的字段默认受检 —— 安全基线是「默认开」，不是「默认关」（§25）。
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def scan_arguments(
        self,
        values: dict[str, Any],
        *,
        rules: dict[str, ParamRule] | None = None,
    ) -> list[InjectionFinding]:
        """扫描一整份参数，返回全部命中（可能为空列表）。"""
        if not self.enabled:
            # 关掉检测属于显式的降级决定（例如离线批处理场景），
            # 调用方应当已经为此打了审计事件；这里只保证行为可预期。
            return []
        findings: list[InjectionFinding] = []
        for field, value in (values or {}).items():
            rule = rules.get(field) if rules else None
            if rule is not None and not rule.injection_check:
                continue
            self._walk(field, value, findings, depth=0)
        return _dedupe(findings)

    def scan_value(self, field: str, value: Any) -> list[InjectionFinding]:
        """扫描单个参数值（不套用任何 ``ParamRule``）。"""
        if not self.enabled:
            return []
        findings: list[InjectionFinding] = []
        self._walk(field, value, findings, depth=0)
        return _dedupe(findings)

    def assert_clean(
        self,
        values: dict[str, Any],
        *,
        rules: dict[str, ParamRule] | None = None,
    ) -> None:
        """命中任一注入特征就抛 :class:`InjectionDetected`（§24 / §34）。

        为什么是抛异常而不是返回 False：注入是**安全事件**，不是「参数写错了」。
        它必须打断正常流程、进入审计与告警通道，绝不能被当成可重试的失败
        （``InjectionDetected.retryable = False``）。
        """
        findings = self.scan_arguments(values, rules=rules)
        if not findings:
            return
        high = [f for f in findings if f.is_high]
        summary = "; ".join(str(f) for f in findings[:3])
        raise InjectionDetected(
            f"参数注入检测未通过（{len(findings)} 处命中，其中 high {len(high)} 处）：{summary}",
            detail={
                "findings": [f.to_dict() for f in findings],
                "high_count": len(high),
            },
        )

    # ------------------------------------------------------------------
    # 遍历
    # ------------------------------------------------------------------
    def _walk(
        self,
        field: str,
        value: Any,
        findings: list[InjectionFinding],
        *,
        depth: int,
    ) -> None:
        if depth > _MAX_WALK_DEPTH:
            return
        if isinstance(value, str):
            if value.strip():
                self._scan_text(field, value, findings)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                self._walk(f"{field}.{key}", item, findings, depth=depth + 1)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for index, item in enumerate(value):
                self._walk(f"{field}[{index}]", item, findings, depth=depth + 1)
            return
        # int / float / bool / None：没有承载注入的载体，直接放过。
        # 注意数值字段的「越界」是 §25 的数值约束管的，不是注入检测管的事。

    def _scan_text(self, field: str, text: str, findings: list[InjectionFinding]) -> None:
        """按**字段语义**分派检测器。

        五个检测器的适用范围并不相同：

        ==================== ==========================================================
        检测器                适用范围
        ==================== ==========================================================
        路径穿越              所有字符串字段（``../`` 在任何语义下都不是正当输入）
        命令注入              仅 command/cmd/shell/… 语义字段（``|`` ``>`` 在正则/表达式里合法）
        SQL 注入              仅 sql/where/… 语义字段（``sleep(`` 在 Python 里合法）
        SSRF                   所有字符串字段，但裸内网 IP 只在 host 语义字段才算命中
        Prompt 注入           所有字符串字段
        ==================== ==========================================================

        宁可**少查**也不要**乱查**：一次误报就会让使用者关掉整个 ``injection_check``，
        那才是真正的漏防。
        """
        self._scan_path_traversal(field, text, findings)
        self._scan_command_injection(field, text, findings)
        self._scan_sql_injection(field, text, findings)
        self._scan_ssrf(field, text, findings)
        self._scan_prompt_injection(field, text, findings)

    # ------------------------------------------------------------------
    # 1) Path Traversal
    # ------------------------------------------------------------------
    def _scan_path_traversal(
        self, field: str, text: str, findings: list[InjectionFinding]
    ) -> None:
        if _RE_NULL_BYTE.search(text):
            findings.append(
                self._add(
                    field,
                    CATEGORY_PATH_TRAVERSAL,
                    SEVERITY_HIGH,
                    text,
                    _RE_NULL_BYTE.search(text),  # type: ignore[arg-type]
                    "含 NUL 字节：可截断底层 C 实现的路径检查（经典绕过）",
                )
            )

        decoded = _deep_unquote(text)
        candidates = [text] if decoded == text else [text, decoded]

        for candidate in candidates:
            posix = candidate.replace("\\", "/")

            if _RE_DEVICE_PATH.match(candidate):
                findings.append(
                    self._make(
                        field,
                        CATEGORY_PATH_TRAVERSAL,
                        SEVERITY_HIGH,
                        candidate,
                        r"\\?\ 设备路径前缀：绕过 Win32 路径规范化",
                    )
                )
            if _RE_UNC.match(posix):
                findings.append(
                    self._make(
                        field,
                        CATEGORY_PATH_TRAVERSAL,
                        SEVERITY_HIGH,
                        candidate,
                        "UNC 路径（\\\\server\\share）：可读写远端主机共享目录",
                    )
                )
            if _RE_WINDOWS_DRIVE.match(candidate):
                findings.append(
                    self._make(
                        field,
                        CATEGORY_PATH_TRAVERSAL,
                        SEVERITY_HIGH,
                        candidate,
                        "Windows 盘符绝对路径：可指向工作区外的任意盘",
                    )
                )

            normalized = _to_posix(candidate)
            segments = [seg for seg in normalized.split("/") if seg]
            if ".." in segments:
                findings.append(
                    self._make(
                        field,
                        CATEGORY_PATH_TRAVERSAL,
                        SEVERITY_HIGH,
                        candidate,
                        f"路径穿越：归一化后仍为 {normalized!r}，跳出工作区根目录",
                    )
                )
            elif _RE_DOTDOT_BYPASS.search(candidate):
                # ``....//....//etc/passwd`` 归一化后不含 ".."（``....`` 是合法文件名），
                # 但 Shell/部分库会把它折叠回 "../" —— 单独判一次。
                findings.append(
                    self._make(
                        field,
                        CATEGORY_PATH_TRAVERSAL,
                        SEVERITY_HIGH,
                        candidate,
                        "多点斜杠绕过（....//）：部分实现会折叠回 ../",
                    )
                )

            if normalized.startswith("/") and not _under_safe_root(normalized):
                # 绝对路径不一定是攻击，但它**不在**允许的工作区根下，
                # 属于「逃逸」的可疑信号，记为 medium 交由策略层再判
                # （allowed_prefix / denied_prefix 才是最终裁决者 §25）。
                findings.append(
                    self._make(
                        field,
                        CATEGORY_PATH_TRAVERSAL,
                        SEVERITY_MEDIUM,
                        candidate,
                        f"绝对路径 {normalized!r} 不在工作区根 {_SAFE_PATH_ROOTS} 之下",
                    )
                )

    # ------------------------------------------------------------------
    # 2) Command Injection
    # ------------------------------------------------------------------
    def _scan_command_injection(
        self, field: str, text: str, findings: list[InjectionFinding]
    ) -> None:
        commandish = _is_commandish(field)
        # 命令语义字段：一律 high —— 这个参数本来就会被丢给 shell 执行。
        # 其它字段：strong 特征记为 medium —— 可能只是恰好出现的符号，
        # 需要人看一眼，但不足以单独断言「这是攻击」。
        strong_severity = SEVERITY_HIGH if commandish else SEVERITY_MEDIUM

        for pattern, detail in _RE_CMD_STRONG:
            match = pattern.search(text)
            if match:
                findings.append(
                    self._add(
                        field, CATEGORY_COMMAND_INJECTION, strong_severity, text, match, detail
                    )
                )

        if not commandish:
            return

        for pattern, detail in _RE_CMD_WEAK + _RE_CMD_SHELL_LINE:
            match = pattern.search(text)
            if match:
                findings.append(
                    self._add(
                        field, CATEGORY_COMMAND_INJECTION, SEVERITY_HIGH, text, match, detail
                    )
                )

    # ------------------------------------------------------------------
    # 3) SQL Injection
    # ------------------------------------------------------------------
    def _scan_sql_injection(
        self, field: str, text: str, findings: list[InjectionFinding]
    ) -> None:
        # 只查 SQL 语义字段。SQL 的特征串（`sleep(` / `--` / `; DROP`）在别的语言里
        # 是正当语法，全域扫描会把 `execute_python.code = "import time; time.sleep(1)"`
        # 判成时间盲注 —— 一次误报就足以让人关掉整条检测，那才是真漏防。
        # 见 `_SQLISH_TOKENS` 上的讨论。
        if not _is_sqlish(field):
            return
        for pattern, detail in _RE_SQL_STRONG:
            match = pattern.search(text)
            if match:
                findings.append(
                    self._add(field, CATEGORY_SQL_INJECTION, SEVERITY_HIGH, text, match, detail)
                )
        for pattern, detail in _RE_SQL_MEDIUM:
            match = pattern.search(text)
            if match:
                findings.append(
                    self._add(field, CATEGORY_SQL_INJECTION, SEVERITY_MEDIUM, text, match, detail)
                )

        # 结构性异常：单引号不成对，说明 SQL 字符串字面量被截断了 ——
        # 这是「拼接 SQL」最典型的指纹（正常业务值几乎不会单独出现奇数个引号）。
        if text.count("'") % 2 == 1:
            findings.append(
                self._make(
                    field,
                    CATEGORY_SQL_INJECTION,
                    SEVERITY_MEDIUM,
                    text,
                    "单引号不成对：疑似 SQL 字符串被截断后拼接",
                )
            )

    # ------------------------------------------------------------------
    # 4) SSRF
    # ------------------------------------------------------------------
    def _scan_ssrf(self, field: str, text: str, findings: list[InjectionFinding]) -> None:
        # (a) 带协议的 URL —— 任何字段里出现都要查
        for match in _RE_URL.finditer(text):
            scheme = match.group(1).lower()
            if scheme in _DANGEROUS_SCHEMES:
                findings.append(
                    self._add(
                        field,
                        CATEGORY_SSRF,
                        SEVERITY_HIGH,
                        text,
                        match,
                        f"危险协议 {scheme}://：{_DANGEROUS_SCHEMES[scheme]}",
                    )
                )
                continue
            if scheme not in ("http", "https", "tcp", "udp", "ws", "wss"):
                continue
            host = _host_of(match.group(2))
            if host is None:
                continue
            reason = _internal_reason(host)
            if reason:
                findings.append(
                    self._add(
                        field,
                        CATEGORY_SSRF,
                        SEVERITY_HIGH,
                        text,
                        match,
                        f"SSRF：请求 {host!r} —— {reason}",
                    )
                )

        # (b) 裸地址：只在 host 语义字段里查。
        # 否则 ``query: "192.168.1.1 是什么"`` 这种正常检索词会被误判成内网探测。
        if not _is_hostish(field):
            return

        for match in _RE_HOST_LITERAL.finditer(text):
            findings.append(
                self._add(
                    field,
                    CATEGORY_SSRF,
                    SEVERITY_HIGH,
                    text,
                    match,
                    f"SSRF：host 类参数直接指向 {match.group(0)!r}",
                )
            )

        for match in _RE_IPV4.finditer(text):
            ip = match.group(1)
            reason = _internal_reason(ip)
            if reason:
                findings.append(
                    self._add(
                        field,
                        CATEGORY_SSRF,
                        SEVERITY_HIGH,
                        text,
                        match,
                        f"SSRF：host 类参数指向内网地址 {ip}（{reason}）",
                    )
                )

        for token in re.findall(r"[0-9A-Za-z]{7,12}", text):
            ip = _decode_obfuscated_ip(token)
            if ip is None:
                continue
            reason = _internal_reason(ip)
            if reason:
                findings.append(
                    self._make(
                        field,
                        CATEGORY_SSRF,
                        SEVERITY_MEDIUM,
                        text,
                        f"整数形式编码的 IP {token!r} 解析为 {ip}（{reason}）—— 常见过滤器绕过手法",
                    )
                )

    # ------------------------------------------------------------------
    # 5) Prompt Injection
    # ------------------------------------------------------------------
    def _scan_prompt_injection(
        self, field: str, text: str, findings: list[InjectionFinding]
    ) -> None:
        for pattern, detail in _RE_PROMPT_STRONG:
            match = pattern.search(text)
            if match:
                findings.append(
                    self._add(
                        field, CATEGORY_PROMPT_INJECTION, SEVERITY_HIGH, text, match, detail
                    )
                )
        for pattern, detail in _RE_PROMPT_MEDIUM:
            match = pattern.search(text)
            if match:
                findings.append(
                    self._add(
                        field, CATEGORY_PROMPT_INJECTION, SEVERITY_MEDIUM, text, match, detail
                    )
                )

    # ------------------------------------------------------------------
    # 构造 helper
    # ------------------------------------------------------------------
    @staticmethod
    def _make(
        field: str,
        category: str,
        severity: str,
        text: str,
        detail: str,
    ) -> InjectionFinding:
        return InjectionFinding(
            field=field,
            category=category,
            severity=severity,
            evidence=_truncate_evidence(text),
            detail=detail,
        )

    @staticmethod
    def _add(
        field: str,
        category: str,
        severity: str,
        text: str,
        match: re.Match[str],
        detail: str,
    ) -> InjectionFinding:
        return InjectionFinding(
            field=field,
            category=category,
            severity=severity,
            evidence=_snippet(text, match),
            detail=detail,
        )


# ======================================================================
# 模块级 helper
# ======================================================================
def _under_safe_root(normalized_path: str) -> bool:
    """路径是否落在 :data:`_SAFE_PATH_ROOTS` 之下（按路径分段比较，避免前缀误判）。"""
    for root in _SAFE_PATH_ROOTS:
        if normalized_path == root or normalized_path.startswith(root + "/"):
            return True
    return False


def _host_of(authority: str) -> Optional[str]:
    """从 URL 的 authority 里抠出主机名（去掉 userinfo 与端口）。"""
    try:
        return urlsplit(f"//{authority}").hostname
    except ValueError:  # pragma: no cover - 病态 URL
        return None


def _internal_reason(host: str) -> Optional[str]:
    """主机名是否为内网/回环/元数据，返回原因。"""
    low = host.lower().strip("[]")
    if low in ("localhost", "localhost.localdomain", "metadata.google.internal",
               "metadata.azure.com", "::1", "0.0.0.0"):
        if low.startswith("metadata."):
            return "云元数据服务域名（可直接窃取实例凭证）"
        return "本机/回环主机名"
    if low.endswith(".internal") or low.endswith(".local"):
        return "内网域名后缀"
    return _is_internal_ip(low)


def _dedupe(findings: list[InjectionFinding]) -> list[InjectionFinding]:
    """按 (字段, 分类, 证据) 去重 —— 同一段文本被两条正则命中时不该报两次。"""
    seen: set[tuple[str, str, str]] = set()
    unique: list[InjectionFinding] = []
    for finding in findings:
        key = (finding.field, finding.category, finding.evidence)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique
