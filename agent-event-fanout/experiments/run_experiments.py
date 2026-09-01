"""设计说明书 §46 —— 8 个实验一键验收（不依赖 pytest）。

以「内存 SQLite + 内存队列 + 内存 Mock 客户」跑完整链路，
打印每一步的可观测证据。

用法：
    python experiments/run_experiments.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import EventFanoutConfig  # noqa: E402
from app.domain.delivery import DLQ, RETRYING, SUCCESS  # noqa: E402
from app.factory import build_runtime  # noqa: E402
from app.webhook.retry import RetryPolicy  # noqa: E402

from examples_util import MockWebhook, seed_subscriber  # noqa: E402

SECRET = "whsec_demo"


async def new_runtime(retry: RetryPolicy | None = None):
    config = EventFanoutConfig(
        database_url="sqlite:///:memory:",
        queue_backend="memory",
        retry=retry or RetryPolicy(max_attempts=5, base_delay=0.0, max_delay=0.0, jitter=0.0),
        request_timeout=1.0,
    )
    rt = await build_runtime(config)
    return rt


async def bind(rt, mock: MockWebhook) -> None:
    rt.sender._client = httpx.AsyncClient(
        timeout=rt.config.request_timeout,
        transport=httpx.MockTransport(mock.handler),
    )


async def first_delivery(rt):
    return (await rt.repo.list_deliveries())[0]


async def force_due(rt, delivery_id: str) -> None:
    from datetime import datetime, timedelta, timezone

    delivery = await rt.repo.get_delivery(delivery_id)
    delivery.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await rt.repo.update_delivery(delivery)


async def publish(rt, type_: str = "agent.job.completed", data: dict | None = None):
    evt = await rt.event_service.create_event(type_, data or {"job_id": "job_demo"})
    await rt.outbox_worker.drain_once()
    await rt.fanout_consumer.drain_once()
    return evt


async def main() -> None:
    report: list[str] = []
    line = "=" * 58

    # 实验 1：Fan-out --------------------------------------------------------
    mock = MockWebhook(secret=SECRET)
    rt = await new_runtime()
    await bind(rt, mock)
    for name in ("crm", "ticket", "slack"):
        await seed_subscriber(rt, url=f"http://mock/{name}", secret=SECRET)
    await publish(rt)
    await rt.webhook_worker.process_due()
    ds = await rt.repo.list_deliveries()
    report.append(f"[实验1] Fan-out: 1 事件 -> {len(ds)} 条 Delivery，全部 {ds[0].status}")
    await rt.stop()

    # 实验 2：Exponential Backoff ---------------------------------------------
    mock = MockWebhook(secret=SECRET).set_fail_then_ok(fail_count=2)
    rt = await new_runtime(
        RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=300.0, jitter=0.0)
    )
    await bind(rt, mock)
    await seed_subscriber(rt, secret=SECRET)
    await publish(rt)
    await rt.webhook_worker.process_due()  # attempt1 -> 503
    d = await first_delivery(rt)
    deltas = [round((d.next_retry_at - d.updated_at).total_seconds(), 1)]
    await force_due(rt, d.id)
    await rt.webhook_worker.process_due()  # attempt2 -> 503
    d = await rt.repo.get_delivery(d.id)
    deltas.append(round((d.next_retry_at - d.updated_at).total_seconds(), 1))
    await force_due(rt, d.id)
    await rt.webhook_worker.process_due()  # attempt3 -> 200
    d = await rt.repo.get_delivery(d.id)
    report.append(f"[实验2] 指数退避: 重试间隔 = {deltas} -> 最终 {d.status}")
    await rt.stop()

    # 实验 3：Retry-After ------------------------------------------------------
    mock = MockWebhook(secret=SECRET).set_ratelimit(retry_after=30)
    rt = await new_runtime()
    await bind(rt, mock)
    await seed_subscriber(rt, secret=SECRET)
    await publish(rt)
    await rt.webhook_worker.process_due()
    d = await first_delivery(rt)
    report.append(f"[实验3] Retry-After: 429 时下次重试 = now+{(d.next_retry_at - d.updated_at).total_seconds():.0f}s")
    await rt.stop()

    # 实验 4：DLQ --------------------------------------------------------------
    mock = MockWebhook(secret=SECRET).set_fail(status=503)
    rt = await new_runtime()
    await bind(rt, mock)
    await seed_subscriber(rt, secret=SECRET)
    await publish(rt)
    await rt.webhook_worker.process_due()
    d = await first_delivery(rt)
    for _ in range(4):
        await force_due(rt, d.id)
        await rt.webhook_worker.process_due()
    d = await rt.repo.get_delivery(d.id)
    report.append(f"[实验4] DLQ: 5 次失败后 status={d.status}, attempt_count={d.attempt_count}")
    await rt.stop()

    # 实验 5：Idempotency ------------------------------------------------------
    mock = MockWebhook(secret=SECRET)
    rt = await new_runtime()
    await bind(rt, mock)
    await seed_subscriber(rt, secret=SECRET)
    await publish(rt)
    d = await first_delivery(rt)
    await rt.webhook_worker.deliver_delivery(d.id)   # 消费一次
    await rt.webhook_worker.deliver_delivery(d.id)   # 重复消费
    report.append(f"[实验5] 幂等: 重复消费同一 Delivery，HTTP 实际只发生 {len(mock.received_for(d.id))} 次")
    await rt.stop()

    # 实验 6：At-least-once -----------------------------------------------------
    mock = MockWebhook(secret=SECRET).set_drop()
    rt = await new_runtime()
    await bind(rt, mock)
    await seed_subscriber(rt, secret=SECRET)
    await publish(rt)
    await rt.webhook_worker.process_due()  # 响应丢失
    d = await first_delivery(rt)
    mock.mode = "ok"
    await force_due(rt, d.id)
    await rt.webhook_worker.process_due()  # 重试成功
    got = mock.received_for(d.id)
    same_id = len({r["headers"].get("X-Webhook-ID") for r in got}) == 1
    report.append(f"[实验6] At-least-once: 响应丢失后重试，客户收到 {len(got)} 次；幂等键相同={same_id}")
    await rt.stop()

    # 实验 7：Outbox -------------------------------------------------------------
    rt = await new_runtime()
    await bind(rt, MockWebhook(secret=SECRET))
    # 事件已写库（含 outbox），但 Queue 故障
    evt = await rt.event_service.create_event("agent.job.completed", {"job_id": "job_demo"})
    original_publish = rt.event_queue.publish

    async def broken(payload: str):
        raise ConnectionError("redis down")

    rt.event_queue.publish = broken
    published = await rt.outbox_worker.drain_once()
    pending = await rt.repo.count_outbox_pending()
    # 恢复 Queue 后重新发布，事件不丢失
    rt.event_queue.publish = original_publish
    republished = await rt.outbox_worker.drain_once()
    report.append(
        f"[实验7] Outbox: Queue 故障时发布数={published}（PENDING={pending}，事件不丢），"
        f"恢复后补发={republished}"
    )
    await rt.stop()

    # 实验 8：Signature -----------------------------------------------------------
    from app.security.signature import SignatureError, generate_signature, verify_signature

    body = b'{"id":"evt_001","type":"agent.job.completed"}'
    ts = int(time.time())
    sig = generate_signature(SECRET, ts, body)
    try:
        verify_signature(SECRET, ts, body.replace(b"completed", b"failed"), sig)
        rejected = False
    except SignatureError:
        rejected = True
    report.append(f"[实验8] 签名: 伪造 body -> HMAC 不匹配 -> 拒绝 = {rejected}")

    print(line)
    print("  8 个实验验收报告（设计说明书 §46）")
    print(line)
    for r in report:
        print(" " + r)
    print(line)


if __name__ == "__main__":
    asyncio.run(main())
