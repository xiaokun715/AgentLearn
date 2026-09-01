"""模拟 Agent 完成任务后向事件系统发布事件（设计说明书 §32, §42~§44）。

用法（先启动 API）：
    uvicorn app.main:app --port 8000

    python examples/agent.py
"""
from __future__ import annotations

import sys
import time

import httpx

API = "http://localhost:8000"


def post_event(type_: str, data: dict, metadata: dict | None = None) -> dict:
    # trust_env=False：直连本机 API，不走系统代理（避免被本地代理拦成 503）
    resp = httpx.post(
        f"{API}/v1/events",
        json={"type": type_, "data": data, "metadata": metadata or {}},
        timeout=10,
        trust_env=False,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    job_id = f"job_{int(time.time())}"
    print(f"[agent] 任务 {job_id} 开始……")

    # §42：Agent Job -> 事件。事件里带上 config/prompt/model 版本（§43）
    # 和 token 用量（§44），形成完整可追溯链路。
    post_event("agent.job.created", {"job_id": job_id}, {"agent": "test_case_agent"})
    post_event(
        "agent.job.running",
        {"job_id": job_id},
        {"config_version": 13, "prompt_version": 20, "model": "qwen3.5-27b"},
    )

    # 模拟 Agent 工作……
    time.sleep(1)

    post_event(
        "agent.job.completed",
        {
            "job_id": job_id,
            "status": "completed",
            "result": {"answer": "这是测试用例生成结果。"},
            "usage": {"input_tokens": 12000, "output_tokens": 3000, "cost": 0.18},
        },
        {"agent": "test_case_agent", "config_version": 13, "prompt_version": 20,
         "model": "qwen3.5-27b"},
    )
    print(f"[agent] 事件已发布。订阅方将异步收到 Webhook（CRM / 工单 / Slack）。")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        print(f"[agent] 发布失败（API 未启动？）: {exc}", file=sys.stderr)
        sys.exit(1)
