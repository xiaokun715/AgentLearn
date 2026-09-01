"""设计说明书 §37：「慢客户」—— 睡 20 秒再返回。

配合客户端 ``request_timeout=5`` 观察：
    timeout -> retry（Webhook Timeout 本质上也是一种失败）。

启动：
    uvicorn slow_customer:app --port 9004
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("slow_customer")

app = FastAPI()


@app.post("/webhook")
async def webhook():
    logger.info("开始慢处理（20s）……")
    await asyncio.sleep(20)
    logger.info("处理完成")
    return {"ok": True}
