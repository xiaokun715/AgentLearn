"""示例：向沙箱提交一段正常 Python 代码（设计说明书 §2 Demo）。

先启动服务：
    uvicorn app.main:app --port 8000

再运行本脚本：
    python examples/python.py
"""
from __future__ import annotations

import sys
import time

import httpx

BASE_URL = "http://127.0.0.1:8000"

# trust_env=False：不走环境变量里的 HTTP(S)_PROXY，直连本地沙箱
_client = httpx.Client(trust_env=False, timeout=30.0)


def submit(type_: str, code: str, policy: dict | None = None) -> dict:
    resp = _client.post(
        f"{BASE_URL}/v1/executions",
        headers={"x-tenant-id": "tenant_a", "x-agent-id": "agent_001"},
        json={"type": type_, "code": code, "policy": policy},
    )
    resp.raise_for_status()
    execution_id = resp.json()["execution_id"]

    # 轮询直到终态
    for _ in range(200):
        view = _client.get(f"{BASE_URL}/v1/executions/{execution_id}").json()
        if view["status"] in (
            "succeeded", "failed", "rejected", "timeout", "oom", "killed",
            "output_limit_exceeded",
        ):
            return view
        time.sleep(0.1)
    raise TimeoutError("execution did not finish in time")


if __name__ == "__main__":
    demo_code = (
        "import math\n"
        "result = sum(math.sqrt(i) for i in range(100))\n"
        "print(result)\n"
    )
    view = submit("python", demo_code)
    print(f"status    : {view['status']}")
    print(f"stdout    : {view['stdout']!r}")
    print(f"stderr    : {view['stderr']!r}")
    print(f"exit_code : {view['exit_code']}")
    print(f"duration  : {view['duration_ms']} ms")
    print(f"resources : {view['resource_usage']}")
    sys.exit(0 if view["status"] == "succeeded" else 1)
