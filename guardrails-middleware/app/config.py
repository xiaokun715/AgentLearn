"""全局配置（设计说明书 §30~§32 API、§36 MVP 范围）。

零外部依赖：全部使用内存态存储（Audit / Approval），策略从 ``configs/`` 下的
YAML 加载。可通过环境变量覆盖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"


@dataclass(slots=True)
class GuardrailsConfig:
    # 策略配置文件目录（默认 <repo>/configs），包含 guardrails.yaml / policies.yaml / tools.yaml
    config_dir: Path = DEFAULT_CONFIG_DIR

    # Human Approval 票据有效期（秒），过期自动转 EXPIRED（§28）
    approval_ttl_seconds: int = 900

    # Audit 内存环形缓冲上限（§29）
    audit_max_events: int = 5000

    # Metrics 延迟统计窗口（保留最近 N 个样本用于 P50/P95/P99，§38）
    latency_window: int = 1000

    log_level: str = "INFO"

    @property
    def tenant_id(self) -> str:
        return "tenant_001"

    @classmethod
    def from_env(cls) -> "GuardrailsConfig":
        return cls(
            config_dir=Path(os.getenv("GUARDRAILS_CONFIG_DIR", str(DEFAULT_CONFIG_DIR))),
            approval_ttl_seconds=int(os.getenv("GUARDRAILS_APPROVAL_TTL", "900")),
            audit_max_events=int(os.getenv("GUARDRAILS_AUDIT_MAX", "5000")),
            latency_window=int(os.getenv("GUARDRAILS_LATENCY_WINDOW", "1000")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


__all__ = ["GuardrailsConfig", "DEFAULT_CONFIG_DIR"]
