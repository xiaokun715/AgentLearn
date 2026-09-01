"""设计说明书 §36：「随机失败客户」—— 50% 概率返回 503。

观察：Attempt 1 -> 503 / Attempt 2 -> 503 / Attempt 3 -> 200，最终 SUCCESS。

启动：
    uvicorn random_failing_customer:app --port 9003
"""
from __future__ import annotations

import logging
import random

from fastapi import FastAPI, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("random_failing_customer")

app = FastAPI()
FAIL_PROBABILITY = 0.5


@app.post("/webhook")
async def webhook():
    if random.random() < FAIL_PROBABILITY:
        logger.warning("随机失败：返回 503")
        return Response(status_code=503, content="boom")
    logger.info("成功：返回 200")
    return {"ok": True}
