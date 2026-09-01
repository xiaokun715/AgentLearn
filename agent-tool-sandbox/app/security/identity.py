"""身份（Identity）上下文（设计说明书 §33 Audit 的前置条件）。

每一次 Tool Execution 都必须能回答：是谁（tenant / user / agent）在跑这份代码。
身份不参与鉴权（第一版不鉴权），只用于审计与隔离标记。
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TENANT = "tenant_default"
ANONYMOUS = "anonymous"


@dataclass(frozen=True, slots=True)
class Identity:
    tenant_id: str
    user_id: str = ANONYMOUS
    agent_id: str = ANONYMOUS

    @property
    def audit_scope(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
        }


def identity_from_headers(headers: dict | None) -> Identity:
    """从 HTTP 头提取身份。缺省值保证审计字段永远非空。"""
    headers = headers or {}
    return Identity(
        tenant_id=headers.get("x-tenant-id") or DEFAULT_TENANT,
        user_id=headers.get("x-user-id") or ANONYMOUS,
        agent_id=headers.get("x-agent-id") or ANONYMOUS,
    )
