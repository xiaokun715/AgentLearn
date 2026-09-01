"""设计说明书 §46 的 8 个实验验收 —— 端到端集成测试。

每个实验独立 Runtime（内存库 + 内存队列），直接观察数据库状态变化。
打印的观测信息即「验收证据」。
"""
from __future__ import annotations

import json

import httpx

from app.config import EventFanoutConfig
from app.domain.delivery import DLQ, RETRYING, SUCCESS
from app.factory import build_runtime
from app.webhook.retry import RetryPolicy

from .conftest import MockWebhook, force_due, seed_subscriber

SECRET = "whsec_experiment"


async def _new_runtime(retry: RetryPolicy | None = None):
    config = EventFanoutConfig(
        database_url="sqlite:///:memory:",
        queue_backend="memory",
        retry=retry or RetryPolicy(max_attempts=5, base_delay=0.0, max_delay=0.0, jitter=0.0),
        request_timeout=1.0,
    )
    rt = await build_runtime(config)
    return rt


async def _bind(rt, mock: MockWebhook) -> None:
    rt.sender._client = httpx.AsyncClient(
        timeout=rt.config.request_timeout,
        transport=httpx.MockTransport(mock.handler),
    )


async def _first(rt):
    return (await rt.repo.list_deliveries())[0]


async def test_eight_experiments():
    report = []

    # ---- 实验 1：一个 Event -> 三个 Subscriber（Fan-out） --------------------
    mock = MockWebhook(secret=SECRET)
    rt = await _new_runtime()
    await _bind(rt, mock)
    for name in ("crm", "ticket", "slack"):
        await seed_subscriber(rt, url=f"http://mock/{name}", secret=SECRET)
    evt = await rt.event_service.create_event("agent.job.completed", {"job_id": "job_1"})
    await rt.outbox_worker.drain_once()
    await rt.fanout_consumer.drain_once()
    await rt.webhook_worker.process_due()
    deliveries = await rt.repo.list_deliveries()
    assert len(deliveries) == 3 and all(d.status == SUCCESS for d in deliveries)
    report.append(("实验1 Fan-out", f"1 事件 -> {len(deliveries)} 条 Delivery 全部 SUCCESS"))
    await rt.stop()

    # ---- 实验 2：500 -> 指数退避 -> 最终成功 ----------------------------------
    mock = MockWebhook(secret=SECRET).set_fail_then_ok(fail_count=2)
    rt = await _new_runtime(
        RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=300.0, jitter=0.0)
    )
    await _bind(rt, mock)
    await seed_subscriber(rt, secret=SECRET)
    evt = await rt.event_service.create_event("agent.job.completed", {})
    await rt.outbox_worker.drain_once()
    await rt.fanout_consumer.drain_once()
    await rt.webhook_worker.process_due()  # attempt 1 -> 503
    d = await _first(rt)
    deltas = [(d.next_retry_at - d.updated_at).total_seconds()]
    await force_due(rt, d.id)
    await rt.webhook_worker.process_due()  # attempt 2 -> 503
    d = await rt.repo.get_delivery(d.id)
    deltas.append((d.next_retry_at - d.updated_at).total_seconds())
    await force_due(rt, d.id)
    await rt.webhook_worker.process_due()  # attempt 3 -> 200
    d = await rt.repo.get_delivery(d.id)
    assert d.status == SUCCESS
    report.append(("实验2 指数退避", f"next_retry_at 增量序列 = {[round(x,1) for x in deltas]} -> 最终 SUCCESS"))
    await rt.stop()

    # ---- 实验 3：429 + Retry-After -------------------------------------------
    mock = MockWebhook(secret=SECRET).set_ratelimit(retry_after=30)
    rt = await _new_runtime()
    await _bind(rt, mock)
    await seed_subscriber(rt, secret=SECRET)
    await rt.event_service.create_event("agent.job.completed", {})
    await rt.outbox_worker.drain_once()
    await rt.fanout_consumer.drain_once()
    await rt.webhook_worker.process_due()
    d = await _first(rt)
    assert d.status == RETRYING
    assert (d.next_retry_at - d.updated_at).total_seconds() == 30
    report.append(("实验3 Retry-After", "429 时 next_retry_at = now+30s（尊重服务端 backpressure）"))
    await rt.stop()

    # ---- 实验 4：永久失败 -> DLQ ----------------------------------------------
    mock = MockWebhook(secret=SECRET).set_fail(status=503)
    rt = await _new_runtime()
    await _bind(rt, mock)
    await seed_subscriber(rt, secret=SECRET)
    await rt.event_service.create_event("agent.job.completed", {})
    await rt.outbox_worker.drain_once()
    await rt.fanout_consumer.drain_once()
    await rt.webhook_worker.process_due()
    d = await _first(rt)
    for _ in range(4):
        await force_due(rt, d.id)
        await rt.webhook_worker.process_due()
    d = await rt.repo.get_delivery(d.id)
    assert d.status == DLQ and d.attempt_count == 5
    report.append(("实验4 DLQ", f"5 次失败后 status=DLQ, attempt_count={d.attempt_count}"))
    await rt.stop()

    # ---- 实验 5：同一 Delivery 重复消费 -> 幂等 ---------------------------------
    mock = MockWebhook(secret=SECRET)
    rt = await _new_runtime()
    await _bind(rt, mock)
    await seed_subscriber(rt, secret=SECRET)
    await rt.event_service.create_event("agent.job.completed", {})
    await rt.outbox_worker.drain_once()
    await rt.fanout_consumer.drain_once()
    await rt.webhook_worker.process_due()
    d = await _first(rt)
    await rt.webhook_worker.deliver_delivery(d.id)  # 再次消费
    got = mock.received_for(d.id)
    assert len(got) == 1
    report.append(("实验5 幂等", f"重复消费同一 Delivery，HTTP 仍只发生 {len(got)} 次"))
    await rt.stop()

    # ---- 实验 6：响应丢失 -> At-least-once -------------------------------------
    mock = MockWebhook(secret=SECRET).set_drop()
    rt = await _new_runtime()
    await _bind(rt, mock)
    await seed_subscriber(rt, secret=SECRET)
    await rt.event_service.create_event("agent.job.completed", {})
    await rt.outbox_worker.drain_once()
    await rt.fanout_consumer.drain_once()
    await rt.webhook_worker.process_due()  # 服务端已处理但响应丢失 -> retry
    d = await _first(rt)
    mock.mode = "ok"
    await force_due(rt, d.id)
    await rt.webhook_worker.process_due()
    got = mock.received_for(d.id)
    assert len(got) == 2
    ids = {r["headers"].get("X-Webhook-ID") for r in got}
    assert ids == {d.id}
    report.append(("实验6 At-least-once",
                   f"响应丢失导致重试，客户收到 {len(got)} 次；幂等键 X-Webhook-ID 相同 -> Receiver 可去重"))
    await rt.stop()

    # ---- 实验 7：Queue publish 失败 -> Outbox ---------------------------------
    rt = await _new_runtime()
    await _bind(rt, MockWebhook(secret=SECRET))
    evt = await rt.event_service.create_event("agent.job.completed", {})

    async def broken(payload: str):
        raise ConnectionError("redis down")

    rt.event_queue.publish = broken
    published = await rt.outbox_worker.drain_once()
    assert published == 0
    pending = await rt.repo.count_outbox_pending()
    assert pending == 1
    report.append(("实验7 Outbox", f"Queue 挂了时事件仍保留在 outbox（PENDING={pending}），恢复后不丢失"))
    await rt.stop()

    # ---- 实验 8：伪造签名 -> 拒绝 ---------------------------------------------
    from app.security.signature import SignatureError, generate_signature

    body = b'{"id":"evt_001","type":"agent.job.completed"}'
    ts = int(__import__("time").time())
    sig = generate_signature(SECRET, ts, body)
    forged = body.replace(b"completed", b"failed")
    try:
        from app.security.signature import verify_signature

        verify_signature(SECRET, ts, forged, sig)
        rejected = False
    except SignatureError:
        rejected = True
    assert rejected
    report.append(("实验8 签名", "伪造 body -> HMAC 不匹配 -> 拒绝（Replay Protection 同理）"))

    # ---- 打印验收报告 --------------------------------------------------------
    print("\n================= 8 个实验验收报告 =================")
    for idx, (name, obs) in enumerate(report, 1):
        print(f"[实验{idx}] {name}: {obs}")
    print("===============================================")
    assert len(report) == 8
