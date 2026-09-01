"""全局配置（设计说明书 §29 技术栈）。

默认零外部依赖可运行：SQLite（持久化）+ 内存队列。
可通过环境变量切换到 PostgreSQL / Redis（见 docker-compose.yml 与 README）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from .webhook.retry import RetryPolicy


@dataclass(slots=True)
class EventFanoutConfig:
    # ---- 存储后端 ------------------------------------------------------------
    # sqlite | postgres
    storage_backend: Literal["sqlite", "postgres"] = "sqlite"
    # sqlite:///./data/fanout.db | postgresql://user:pw@localhost:5432/agent_event_fanout
    database_url: str = "sqlite:///./data/fanout.db"

    # ---- 队列后端 -------------------------------------------------------------
    # memory | redis
    queue_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # ---- Webhook 投递参数 -----------------------------------------------------
    # 设计 §16~§19：重试策略；§37：请求超时
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    request_timeout: float = 10.0
    # 签名时间窗（秒），设计 §14：Replay Protection
    signature_tolerance: int = 300

    # ---- Outbox / Worker 轮询参数 -------------------------------------------
    outbox_poll_interval: float = 0.5
    outbox_batch_size: int = 50
    webhook_poll_interval: float = 0.5
    webhook_batch_size: int = 10

    log_level: str = "INFO"

    @property
    def sqlite_path(self) -> str:
        return self.database_url.replace("sqlite:///", "", 1)

    @property
    def tenant_id(self) -> str:
        return "tenant_001"

    @classmethod
    def from_env(cls) -> "EventFanoutConfig":
        return cls(
            storage_backend=os.getenv("STORAGE_BACKEND", "sqlite"),  # type: ignore[assignment]
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/fanout.db"),
            queue_backend=os.getenv("QUEUE_BACKEND", "memory"),  # type: ignore[assignment]
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", "10")),
            signature_tolerance=int(os.getenv("SIGNATURE_TOLERANCE", "300")),
            outbox_poll_interval=float(os.getenv("OUTBOX_POLL_INTERVAL", "0.5")),
            outbox_batch_size=int(os.getenv("OUTBOX_BATCH_SIZE", "50")),
            webhook_poll_interval=float(os.getenv("WEBHOOK_POLL_INTERVAL", "0.5")),
            webhook_batch_size=int(os.getenv("WEBHOOK_BATCH_SIZE", "10")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
