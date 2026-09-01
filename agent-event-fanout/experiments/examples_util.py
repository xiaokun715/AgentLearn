"""实验脚本公用工具：内存 Mock Webhook 客户 + 订阅者 seed。"""
from __future__ import annotations

import httpx

from app.domain.subscriber import Subscriber
from app.webhook.signer import Signer


class MockWebhook:
    """可配置假客户。mode: ok | always_fail | fail_n_then_ok | ratelimit | drop。"""

    def __init__(self, *, verify_signature: bool = True, secret: str = "whsec_demo") -> None:
        self.requests: list[dict] = []
        self.mode = "ok"
        self.fail_status = 503
        self.fail_count = 1
        self.retry_after = 30
        self.verify_signature = verify_signature
        self.secret = secret
        self.signer = Signer()

    def set_fail(self, *, status: int = 503) -> "MockWebhook":
        self.mode = "always_fail"
        self.fail_status = status
        return self

    def set_fail_then_ok(self, fail_count: int, *, status: int = 503) -> "MockWebhook":
        self.mode = "fail_n_then_ok"
        self.fail_count = fail_count
        self.fail_status = status
        return self

    def set_ratelimit(self, retry_after: int = 30) -> "MockWebhook":
        self.mode = "ratelimit"
        self.retry_after = retry_after
        return self

    def set_drop(self) -> "MockWebhook":
        self.mode = "drop"
        return self

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({
            "headers": request.headers, "body": request.content, "path": request.url.path,
        })
        if self.verify_signature:
            self.signer.verify_request(
                secret=self.secret,
                timestamp=request.headers.get("X-Webhook-Timestamp", ""),
                signature=request.headers.get("X-Webhook-Signature", ""),
                body=request.content,
            )
        if self.mode == "always_fail":
            return httpx.Response(self.fail_status, text="unavailable")
        if self.mode == "fail_n_then_ok":
            if self._count_for(request) <= self.fail_count:
                return httpx.Response(self.fail_status, text="boom")
            return httpx.Response(200, json={"ok": True})
        if self.mode == "ratelimit":
            return httpx.Response(
                429, text="slow down", headers={"Retry-After": str(self.retry_after)}
            )
        if self.mode == "drop":
            raise httpx.ReadTimeout("response lost after processing")
        return httpx.Response(200, json={"ok": True})

    def _count_for(self, request: httpx.Request) -> int:
        delivery_id = request.headers.get("X-Webhook-ID", "")
        return sum(1 for r in self.requests
                   if r["headers"].get("X-Webhook-ID", "") == delivery_id)

    def received_for(self, delivery_id: str) -> list[dict]:
        return [r for r in self.requests
                if r["headers"].get("X-Webhook-ID", "") == delivery_id]


async def seed_subscriber(runtime, *, url: str = "http://mock/crm",
                          events: list[str] | None = None,
                          secret: str = "whsec_demo") -> Subscriber:
    sub = Subscriber.create(
        url=url,
        events=events or ["agent.job.completed"],
        tenant_id=runtime.config.tenant_id,
        secret=secret,
    )
    await runtime.repo.create_subscriber(sub)
    return sub
