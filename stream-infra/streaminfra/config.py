"""StreamInfra 运行时配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class BufferBackend(str, Enum):
    MEMORY = "memory"
    REDIS = "redis"


class BackpressureStrategy(str, Enum):
    BLOCK = "block"          # 策略 A：阻塞 Producer（不丢 Token，Demo 推荐）
    DROP_OLDEST = "drop_oldest"  # 策略 B：丢弃最旧
    DROP_NEWEST = "drop_newest"  # 策略 C：丢弃最新
    DISCONNECT = "disconnect"    # 策略 D：断开慢 Consumer


@dataclass(slots=True)
class StreamConfig:
    # —— 队列 / 背压 ——
    queue_size: int = 100             # 有界队列容量（背压的来源）
    max_queue_wait: float = 30.0      # Producer 入队最大等待；超时则取消流
    backpressure_strategy: BackpressureStrategy = BackpressureStrategy.BLOCK

    # —— 重放缓冲 ——
    max_events: int = 1000            # Replay Buffer 最多保留的事件数
    buffer_backend: BufferBackend = BufferBackend.MEMORY
    redis_url: str = "redis://localhost:6379/0"

    # —— 传输 ——
    heartbeat_interval: float = 15.0  # 长时间无事件时发送心跳的间隔
    poll_interval: float = 0.25       # 队列轮询 / 断线探测间隔
    append_done_sentinel: bool = True # SSE 结尾追加 "data: [DONE]"

    # —— Mock LLM ——
    provider_delay: float = 0.05      # 每个 token 的生成延迟
    provider_idle_timeout: float = 60.0  # 上游超过该时长无事件 -> UPSTREAM_TIMEOUT
    mock_input_tokens: int = 120
    mock_tokens: list[str] = field(default_factory=lambda: list(
        "你好，这是一个 Streaming Demo。它演示了 LLM 流式响应的基础设施："
        "SSE、WebSocket、背压、断线重连与重放。"
    ))
    mock_fail_after: int | None = None    # 可选：第 N 个 token 后上游报错
    mock_tool_call_after: int | None = None  # 可选：第 N 个 token 后插入 tool_call 事件

    @classmethod
    def from_env(cls) -> "StreamConfig":
        def _int(name: str, default: int) -> int:
            return int(os.getenv(name, default))

        def _float(name: str, default: float) -> float:
            return float(os.getenv(name, default))

        cfg = cls()
        cfg.queue_size = _int("STREAM_QUEUE_SIZE", cfg.queue_size)
        cfg.max_events = _int("STREAM_MAX_EVENTS", cfg.max_events)
        cfg.max_queue_wait = _float("STREAM_MAX_QUEUE_WAIT", cfg.max_queue_wait)
        cfg.heartbeat_interval = _float("STREAM_HEARTBEAT_INTERVAL", cfg.heartbeat_interval)
        cfg.poll_interval = _float("STREAM_POLL_INTERVAL", cfg.poll_interval)
        cfg.provider_delay = _float("STREAM_PROVIDER_DELAY", cfg.provider_delay)
        cfg.provider_idle_timeout = _float("STREAM_PROVIDER_IDLE_TIMEOUT", cfg.provider_idle_timeout)
        cfg.buffer_backend = BufferBackend(os.getenv("STREAM_BUFFER_BACKEND", cfg.buffer_backend.value))
        cfg.redis_url = os.getenv("STREAM_REDIS_URL", cfg.redis_url)
        cfg.backpressure_strategy = BackpressureStrategy(
            os.getenv("STREAM_BACKPRESSURE_STRATEGY", cfg.backpressure_strategy.value)
        )
        return cfg
