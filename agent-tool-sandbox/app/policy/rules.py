"""静态规则（Policy Rules）—— 第一层防线（防御纵深的第一层）。

在把代码放进沙箱之前先做静态扫描，可以：
1. 提前拒绝明显恶意代码（快速失败），省下沙箱资源；
2. 对可疑模式给出 warning，交给后续监控（PID limit 等）兜底。

⚠️ 真实安全**不依赖**静态分析 —— 静态分析可被混淆绕过。
真正的隔离由 Sandbox（Docker / Process + 资源限制）保证。这里是 Defense-in-Depth，
不是唯一防线。

本模块产出「发现」（findings），Policy Engine 依据 Policy 决定是否 REJECT。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 每个 capability 对应一组危险调用模式。命中即视为一个 finding。
DANGEROUS_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    # 网络访问（Policy 网络被禁用时，出现这些调用 → REJECT）
    "network": [
        re.compile(r"\brequests\.(get|post|put|delete|patch|head)\b"),
        re.compile(r"urllib\.request\.(urlopen|Request)\b"),
        re.compile(r"http\.client\.(HTTPConnection|HTTPSConnection)\b"),
        re.compile(r"aiohttp\.ClientSession\b"),
        re.compile(r"\bsocket\.socket\b"),
        re.compile(r"xmlrpc\.client\b"),
    ],
    # 宿主机文件系统访问（永远禁止 → REJECT）
    "filesystem_host": [
        re.compile(r"open\(\s*['\"]/etc/passwd"),
        re.compile(r"open\(\s*['\"]/root/"),
        re.compile(r"open\(\s*['\"]/host[_-]"),
        re.compile(r"open\(\s*['\"]/proc/"),
        re.compile(r"open\(\s*['\"]/srv/"),
        re.compile(r"os\.system\b"),
        re.compile(r"shutil\.rmtree\b"),
        re.compile(r"os\.remove\b"),
        re.compile(r"os\.unlink\b"),
    ],
    # 进程 / 系统调用逃逸（沙箱外执行、动态加载、ptrace 等 → REJECT）
    "syscall_escape": [
        re.compile(r"\bos\.fork\b"),
        re.compile(r"ctypes\.(CDLL|cdll)\b"),
        re.compile(r"\bptrace\b"),
        re.compile(r"\bnsenter\b"),
        re.compile(r"\bunshare\b"),
    ],
    # 疑似 fork bomb（while True 无限产生子进程）→ 仅 warning，交给 PID limit 兜底
    "fork_bomb": [
        re.compile(r"while\s+True:.*Popen", re.DOTALL),
        re.compile(r"while\s+True:.*subprocess", re.DOTALL),
    ],
}


@dataclass(slots=True)
class StaticRuleResult:
    """静态扫描结果：capability → 命中的具体片段。"""

    findings: dict[str, list[str]] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return not self.findings

    def reasons(self, capability: str | None = None) -> list[str]:
        if capability is not None:
            return self.findings.get(capability, [])
        return [hit for hits in self.findings.values() for hit in hits]


def scan_code(code: str) -> StaticRuleResult:
    """扫描 Agent 代码，返回所有命中的危险模式。"""
    findings: dict[str, list[str]] = {}
    for capability, patterns in DANGEROUS_PATTERNS.items():
        hits: list[str] = []
        for pattern in patterns:
            match = pattern.search(code)
            if match:
                hits.append(match.group(0))
        if hits:
            findings[capability] = hits
    return StaticRuleResult(findings=findings)
