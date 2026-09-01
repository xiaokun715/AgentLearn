"""真实端到端冒烟：起 app + 真实客户服务器，验证签名校验与幂等。

用法：
    python experiments/e2e_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import httpx

APP_PORT = 8017
CUST_PORT = 9001


def wait_up(url: str, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            # trust_env=False：直连，不走系统代理
            r = httpx.get(url, timeout=1, trust_env=False)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        import time

        time.sleep(0.25)
    return False


async def main() -> None:
    app = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(APP_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    cust: subprocess.Popen | None = None
    try:
        if not wait_up(f"http://localhost:{APP_PORT}/healthz"):
            raise RuntimeError("app 启动失败")
        async with httpx.AsyncClient(
            base_url=f"http://localhost:{APP_PORT}", timeout=5, trust_env=False
        ) as c:
            r = await c.post(
                "/v1/subscribers",
                json={"url": f"http://localhost:{CUST_PORT}/webhook",
                      "events": ["agent.job.completed"]},
            )
            sub = r.json()
            print("订阅者:", sub["id"], "secret:", sub["secret"][:12] + "...")

            cust_env = {**os.environ, "WEBHOOK_SECRET": sub["secret"]}
            cust = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "examples.customer_server:app",
                 "--port", str(CUST_PORT)],
                env=cust_env, cwd=".",
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if not wait_up(f"http://localhost:{CUST_PORT}/processed"):
                raise RuntimeError("客户服务器启动失败")

            r = await c.post(
                "/v1/events",
                json={"type": "agent.job.completed",
                      "data": {"job_id": "job_e2e", "result": {"answer": "hello"}}},
            )
            print("事件:", r.json())

            status, attempts = None, None
            for _ in range(40):
                await asyncio.sleep(0.5)
                ds = (await c.get("/v1/deliveries")).json()
                if ds:
                    status, attempts = ds[0]["status"], ds[0]["attempt_count"]
                    if status in ("SUCCESS", "DLQ"):
                        break
            print("投递:", status, "attempts:", attempts)

            processed = (await httpx.AsyncClient(timeout=3, trust_env=False).get(
                f"http://localhost:{CUST_PORT}/processed")).json()
            print("客户已处理数:", processed)

            assert status == "SUCCESS", f"期望 SUCCESS，实际 {status}"
            assert processed["processed"] == 1, "客户应恰好处理 1 次"
            print(">>> 端到端全链路通过（真实 HTTP + HMAC 校验 + 幂等）")
    finally:
        app.terminate()
        if cust:
            cust.terminate()
        for p in (app, cust):
            if p:
                try:
                    p.wait(timeout=3)
                except Exception:
                    p.kill()


if __name__ == "__main__":
    asyncio.run(main())
