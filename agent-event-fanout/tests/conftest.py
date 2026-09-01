"""共享测试 fixtures —— 内存 SQLite + 内存队列 + 可配置的 Mock Webhook 服务器。

默认零外部依赖；RetryPolicy 用 ``base_delay=0`` 使重试立即到期，测试快速且确定。
"""
from __future__ import annotations

import httpx
import pytest

from app.config import EventFanoutConfig
from app.domain.subscriber import Subscriber
from app.factory import Runtime, build_runtime
from app.webhook.retry import RetryPolicy
from app.webhook.signer import Signer


class MockWebhook:
    """可配置的假客户 Webhook 服务器（httpx.MockTransport）。

    模式:
        ok             -> 200
        always_fail    -> fail_status（默认 503）
        fail_n_then_ok -> 前 fail_count 次失败，之后 200
        ratelimit      -> 429 + Retry-After
        slow           -> 睡 slow_seconds 后 200（配合短超时验证超时重试）
        drop           -> 模拟「服务端已处理但响应丢失」（实验 6）
    """

    def __init__(self, *, verify_signature: bool = True, secret: str = "whsec_test") -> None:
        self.requests: list[dict] = []  # {"headers", "body", "path"}
        self.mode = "ok"
        self.fail_status = 503
        self.fail_count = 1
        self.retry_after = 30
        self.slow_seconds = 0.0
        self.verify_signature = verify_signature
        self.secret = secret
        self.signer = Signer()

    # ---- 便捷配置 -----------------------------------------------------------
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

    def set_slow(self, seconds: float) -> "MockWebhook":
        self.mode = "slow"
        self.slow_seconds = seconds
        return self

    def set_drop(self) -> "MockWebhook":
        self.mode = "drop"
        return self

    # ---- httpx transport handler -------------------------------------------
    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        headers = request.headers
        self.requests.append({"headers": headers, "body": body, "path": request.url.path})

        if self.verify_signature:
            self.signer.verify_request(
                secret=self.secret,
                timestamp=headers.get("X-Webhook-Timestamp", ""),
                signature=headers.get("X-Webhook-Signature", ""),
                body=body,
            )

        if self.mode == "always_fail":
            return httpx.Response(self.fail_status, text="unavailable")
        if self.mode == "fail_n_then_ok":
            delivered = self._count_for(request)
            if delivered <= self.fail_count:
                return httpx.Response(self.fail_status, text="boom")
            return httpx.Response(200, json={"ok": True})
        if self.mode == "ratelimit":
            return httpx.Response(
                429, text="slow down", headers={"Retry-After": str(self.retry_after)}
            )
        if self.mode == "slow":
            import asyncio

            # 模拟真实场景：慢客户 + 客户端超时 -> 发送方看到 ReadTimeout
            await asyncio.sleep(self.slow_seconds)
            raise httpx.ReadTimeout("server too slow, request timed out")
        if self.mode == "drop":
            # 服务端已处理，但响应在返回前「丢失」-> 发送方看到超时/连接错误
            raise httpx.ReadTimeout("response lost after processing")
        return httpx.Response(200, json={"ok": True})

    def _count_for(self, request: httpx.Request) -> int:
        delivery_id = request.headers.get("X-Webhook-ID", "")
        return sum(
            1
            for r in self.requests
            if r["headers"].get("X-Webhook-ID", "") == delivery_id
        )

    def received_for(self, delivery_id: str) -> list[dict]:
        return [
            r for r in self.requests
            if r["headers"].get("X-Webhook-ID", "") == delivery_id
        ]


@pytest.fixture
def mock() -> MockWebhook:
    return MockWebhook()


@pytest.fixture
async def runtime(mock: MockWebhook) -> Runtime:
    """测试用 Runtime：内存库 + 内存队列 + 立即到期的重试策略。"""
    config = EventFanoutConfig(
        database_url="sqlite:///:memory:",
        queue_backend="memory",
        retry=RetryPolicy(
            max_attempts=5,
            base_delay=0.0,
            max_delay=0.0,
            jitter=0.0,
        ),
        request_timeout=1.0,
    )
    rt = await build_runtime(config)
    rt.sender._client = httpx.AsyncClient(
        timeout=config.request_timeout,
        transport=httpx.MockTransport(mock.handler),
    )
    yield rt
    await rt.stop()


# ---- 造数据辅助 -----------------------------------------------------------
async def seed_subscriber(
    runtime: Runtime,
    *,
    url: str = "http://mock/crm",
    events: list[str] | None = None,
    secret: str = "whsec_test",
    status: str = "active",
) -> Subscriber:
    sub = Subscriber.create(
        url=url,
        events=events or ["agent.job.completed"],
        tenant_id=runtime.config.tenant_id,
        secret=secret,
    )
    if status != "active":
        sub.status = status
    await runtime.repo.create_subscriber(sub)
    return sub


async def publish_and_deliver(
    runtime: Runtime,
    type_: str = "agent.job.completed",
    data: dict | None = None,
) -> str:
    """完整走一遍：Event -> Outbox -> Fan-out -> Webhook，返回 event_id。"""
    evt = await runtime.event_service.create_event(type_, data or {"job_id": "job_123"})
    await runtime.outbox_worker.drain_once()
    await runtime.fanout_consumer.drain_once()
    await runtime.webhook_worker.process_due()
    return evt.id


def next_retry_delta(delivery) -> float:
    """next_retry_at - updated_at 的秒数（验证退避/Retry-After）。"""
    from app.storage.models import from_db

    assert delivery.next_retry_at is not None, "该 Delivery 没有安排重试"
    updated = delivery.updated_at
    if updated.tzinfo is None:
        from datetime import timezone

        updated = updated.replace(tzinfo=timezone.utc)
    return (delivery.next_retry_at - updated).total_seconds()


async def force_due(runtime: Runtime, delivery_id: str) -> None:
    """把 Delivery 的 next_retry_at 推到过去，使下一次 process_due 立即领取。"""
    from datetime import datetime, timedelta, timezone

    delivery = await runtime.repo.get_delivery(delivery_id)
    delivery.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await runtime.repo.update_delivery(delivery)


__all__ = [
    "MockWebhook",
    "Runtime",
    "seed_subscriber",
    "publish_and_deliver",
    "next_retry_delta",
    "force_due",
]
