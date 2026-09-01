"""Network Egress（设计说明书 §17-18）。

原则（Network Least Privilege）：
    默认 network = disabled。
    如果需要访问外部，必须显式 allow_domains —— 不允许直接 `network=true`。

架构（V2 用 Egress Proxy 实现真正的域名级放行）：
    Sandbox → Egress Proxy → api.example.com → ALLOW
                            → github.com      → DENY
                            → 10.0.0.1        → DENY

V1 里我们做到策略决策层的严格校验（拒绝私网 / metadata / 空白名单），并把编译结果传给
Docker 的 network_disabled；真正的 per-domain 代理放行留到 Phase V2。

始终禁止的目标（SSRF 防护）：
    169.254.169.254（云 metadata service）
    127.0.0.1 / ::1（回环）
    10.0.0.0/8、172.16.0.0/12、192.168.0.0/16（私网）
    169.254.0.0/16（链路本地）
"""
from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FORBIDDEN_IPS = frozenset({"169.254.169.254", "127.0.0.1", "::1"})

PRIVATE_NETWORKS: list[ipaddress.IPv4Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
]

# 简单域名格式校验：a.b.c（不解析 DNS，第一版不做解析）
HOSTNAME_RE = re.compile(
    r"^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)


@dataclass(slots=True)
class EgressDecision:
    network_enabled: bool
    allow_domains: list[str] = field(default_factory=list)
    denied_domains: list[str] = field(default_factory=list)
    reason: str = ""


class EgressPolicy:
    """决定沙箱的网络隔离级别，并校验 allow-list 的合法性。"""

    def decide(self, enabled: bool, allow_domains: list[str]) -> EgressDecision:
        if not enabled:
            return EgressDecision(
                network_enabled=False, allow_domains=[],
                reason="network disabled by policy",
            )

        # 想要网络，但没给白名单 = network=true，直接禁止（§18）
        if not allow_domains:
            return EgressDecision(
                network_enabled=False, allow_domains=[],
                reason="network=true not allowed; must provide allow_domains",
            )

        denied: list[str] = []
        valid: list[str] = []
        for domain in allow_domains:
            if self._is_forbidden(domain):
                denied.append(domain)
            else:
                valid.append(domain)

        if denied:
            return EgressDecision(
                network_enabled=False, allow_domains=[], denied_domains=denied,
                reason=f"forbidden egress target(s) in allow list: {denied}",
            )

        return EgressDecision(
            network_enabled=True, allow_domains=valid, reason="allow-list ok"
        )

    @staticmethod
    def _is_forbidden(target: str) -> bool:
        """判断目标是否被禁止：IP 字面量（metadata/回环/私网）或非法域名。"""
        host = target.split(":")[0]  # 去掉可能的端口
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None

        if ip is not None:
            if str(ip) in FORBIDDEN_IPS:
                return True
            if any(ip in net for net in PRIVATE_NETWORKS):
                return True
            return False

        # 域名：只做格式校验（不解析）。空 / 非域名 → 禁止。
        return not bool(HOSTNAME_RE.match(host))
