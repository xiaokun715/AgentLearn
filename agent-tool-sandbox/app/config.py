"""全局配置（设计说明书 §5 推荐技术栈 + §40 分阶段演进）。

默认零依赖可运行：SQLite（持久化）+ asyncio 内存执行队列 + ProcessSandbox（无 Docker 兜底）。
可用环境变量切换到 Docker / PostgreSQL：
    SANDBOX_BACKEND=auto|docker|process
    STORAGE_BACKEND=memory|sqlite|postgres
    DATABASE_URL=sqlite:///./data/sandbox.db | postgresql://user:pw@host:5432/db
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class AppConfig:
    """沙箱服务的应用级配置（不是编译后的 SandboxConfig）。"""

    # ---- 存储后端 ------------------------------------------------------------
    # memory | sqlite | postgres
    storage_backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    database_url: str = "sqlite:///./data/sandbox.db"

    # ---- 沙箱后端 -------------------------------------------------------------
    # auto | docker | process   （auto = 有 Docker 用 Docker，否则退化到 process）
    sandbox_backend: Literal["auto", "docker", "process"] = "auto"

    # ---- 执行队列（§5 Execution Queue，生产可用 Redis 替换，这里用 asyncio 信号量）---
    max_concurrency: int = 4

    # ---- 资源监控采样间隔（越小越敏感，代价是更多 psutil/docker stats 调用）----
    monitor_interval: float = 0.25

    # ---- 默认策略 --------------------------------------------------------------
    default_policy: str = "python_basic"

    # ---- 审计 ----------------------------------------------------------------
    audit_enabled: bool = True

    log_level: str = "INFO"

    # ---- 便捷属性 ---------------------------------------------------------------
    @property
    def sqlite_path(self) -> str:
        return self.database_url.replace("sqlite:///", "", 1)

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            storage_backend=os.getenv("STORAGE_BACKEND", "sqlite"),  # type: ignore[assignment]
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/sandbox.db"),
            sandbox_backend=os.getenv("SANDBOX_BACKEND", "auto"),  # type: ignore[assignment]
            max_concurrency=int(os.getenv("MAX_CONCURRENCY", "4")),
            monitor_interval=float(os.getenv("MONITOR_INTERVAL", "0.25")),
            default_policy=os.getenv("DEFAULT_POLICY", "python_basic"),
            audit_enabled=os.getenv("AUDIT_ENABLED", "1") == "1",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
