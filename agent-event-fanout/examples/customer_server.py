"""正常客户 Webhook 服务器（设计说明书 §15, §23）。

启动：
    uvicorn customer_server:app --port 9001

行为：
    1. 校验 HMAC 签名 + 时间窗（§13~§15）
    2. 用 X-Webhook-ID 幂等去重（§23~§24：At-least-once 的关键）
    3. 返回 200
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

try:  # 直接运行（cd examples; uvicorn customer_server:app）
    from verify_webhook import verify_webhook_request
except ModuleNotFoundError:  # 作为包导入
    from .verify_webhook import verify_webhook_request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("customer")

app = FastAPI()

# 与 agent-event-fanout 里注册 Subscriber 时拿到的 secret 一致。
# 可通过 WEBHOOK_SECRET 环境变量覆盖，便于端到端联调。
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "whsec_customer_demo_secret")

# 幂等表：已经处理过的 delivery_id（§23：主键冲突即已处理）
processed: set[str] = set()


@app.post("/webhook")
async def webhook(
    request: Request,
    x_webhook_id: str = Header(None, alias="X-Webhook-ID"),
):
    body = await request.body()

    # 1) 校验签名 + 时间窗（§14 Replay Protection）
    headers = dict(request.headers)
    try:
        delivery_id = verify_webhook_request(
            secret=WEBHOOK_SECRET, headers=headers, body=body
        )
    except Exception as exc:
        logger.warning("签名校验失败: %s", exc)
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # 2) 幂等：同一 delivery_id 只处理一次（§23）
    if delivery_id in processed:
        logger.info("重复投递，幂等跳过: delivery=%s", delivery_id)
        return {"ok": True, "duplicate": True}
    processed.add(delivery_id)

    logger.info(
        "收到事件 delivery=%s event=%s body=%s",
        delivery_id, headers.get("X-Event-ID"), body.decode()[:200],
    )
    return {"ok": True}


@app.get("/processed")
async def processed_count():
    return {"processed": len(processed)}
