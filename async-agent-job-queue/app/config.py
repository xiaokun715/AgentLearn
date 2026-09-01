"""全局配置（设计说明书 §13 演进路线）。

默认零依赖可运行：SQLite（持久化）+ asyncio 内存队列。
可通过环境变量切换到 PostgreSQL / Redis（见 docker-compose.yml 与 README）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class QueueConfig:
    # ---- 存储后端 ------------------------------------------------------------
    # memory | sqlite | postgres
    storage_backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    # sqlite:///./data/jobs.db | postgresql://user:pw@localhost:5432/jobq
    database_url: str = "sqlite:///./data/jobs.db"

    # ---- 队列后端 -------------------------------------------------------------
    # memory | redis
    queue_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    fair_scheduling: bool = False  # §48-49：按 tenant 轮询，防饿死

    # ---- Worker --------------------------------------------------------------
    worker_count: int = 4

    # ---- Lease / Heartbeat（§29-31） -----------------------------------------
    lease_duration: float = 20.0
    heartbeat_interval: float = 5.0

    # ---- Reaper（§30）---------------------------------------------------------
    reaper_interval: float = 5.0
    reaper_grace: float = 1.0  # 租约过期后额外宽限，避免抖动误回收

    # ---- Retry / Backoff（§24-26） --------------------------------------------
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    backoff_jitter: float = 0.5

    log_level: str = "INFO"

    # ---- 便捷属性 ---------------------------------------------------------------
    @property
    def sqlite_path(self) -> str:
        return self.database_url.replace("sqlite:///", "", 1)

    @classmethod
    def from_env(cls) -> "QueueConfig":
        return cls(
            storage_backend=os.getenv("STORAGE_BACKEND", "sqlite"),  # type: ignore[assignment]
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/jobs.db"),
            queue_backend=os.getenv("QUEUE_BACKEND", "memory"),  # type: ignore[assignment]
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            fair_scheduling=os.getenv("FAIR_SCHEDULING", "0") == "1",
            worker_count=int(os.getenv("WORKER_COUNT", "4")),
            lease_duration=float(os.getenv("LEASE_DURATION", "20")),
            heartbeat_interval=float(os.getenv("HEARTBEAT_INTERVAL", "5")),
            reaper_interval=float(os.getenv("REAPER_INTERVAL", "5")),
            reaper_grace=float(os.getenv("REAPER_GRACE", "1")),
            max_retries=int(os.getenv("MAX_RETRIES", "3")),
            backoff_base=float(os.getenv("BACKOFF_BASE", "1")),
            backoff_max=float(os.getenv("BACKOFF_MAX", "30")),
            backoff_jitter=float(os.getenv("BACKOFF_JITTER", "0.5")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
