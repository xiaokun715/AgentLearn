"""设计说明书 §35：「失败客户」—— 永远返回 503。

用途：观察 Event -> 503 -> Retry -> 503 -> ... -> DLQ 的全过程。
观察数据库 ``webhook_deliveries``：``attempt_count = 5, status = DLQ``。

启动：
    uvicorn failing_customer:app --port 9002
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("failing_customer")

app = FastAPI()


@app.post("/webhook")
async def webhook():
    logger.warning("返回 503（永远失败）")
    return Response(status_code=503, content="unavailable")
