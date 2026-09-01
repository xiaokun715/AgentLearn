"""设计说明书 §38：限流客户 —— 返回 429 + Retry-After。

观察：系统尊重 ``Retry-After: 30``，``next_retry_at = now + 30s``，
而不是无脑指数退避（服务端显式 backpressure）。

启动：
    uvicorn ratelimit_customer:app --port 9005
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ratelimit_customer")

app = FastAPI()


@app.post("/webhook")
async def webhook():
    logger.warning("返回 429 + Retry-After: 30")
    return Response(
        status_code=429,
        content="too many requests",
        headers={"Retry-After": "30"},
    )
