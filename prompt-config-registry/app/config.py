"""全局配置（设计说明书 §5 技术栈）。

默认零外部依赖可运行：SQLite（持久化）+ 内存缓存。
可通过环境变量切换到 PostgreSQL / Redis（见 docker-compose.yml 与 README）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class RegistryConfig:
    # ---- 存储后端 ------------------------------------------------------------
    # memory | sqlite | postgres
    storage_backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    # sqlite:///./data/registry.db | postgresql://user:pw@localhost:5432/config_registry
    database_url: str = "sqlite:///./data/registry.db"

    # ---- 缓存后端 -------------------------------------------------------------
    # memory | redis
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl: int = 60  # §23：Deployment / Snapshot 缓存 TTL（秒）

    log_level: str = "INFO"

    @property
    def sqlite_path(self) -> str:
        return self.database_url.replace("sqlite:///", "", 1)

    @classmethod
    def from_env(cls) -> "RegistryConfig":
        return cls(
            storage_backend=os.getenv("STORAGE_BACKEND", "sqlite"),  # type: ignore[assignment]
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/registry.db"),
            cache_backend=os.getenv("CACHE_BACKEND", "memory"),  # type: ignore[assignment]
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            cache_ttl=int(os.getenv("CACHE_TTL", "60")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
