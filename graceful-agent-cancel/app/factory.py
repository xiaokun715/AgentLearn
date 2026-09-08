"""应用装配（工厂模式）—— 按配置把四道防线需要的组件组装成一个 Runtime。"""
from __future__ import annotations

from dataclasses import dataclass

from .cancellation.registry import RunningTaskRegistry
from .config import AgentConfig
from .domain.events import EventBus
from .service import TaskService
from .store.memory import MemoryCancellationStore


@dataclass
class Runtime:
    """一次性持有全部组件，方便 API / 测试 / 脚本共用。"""

    config: AgentConfig
    bus: EventBus
    cancel_store: MemoryCancellationStore
    registry: RunningTaskRegistry
    service: TaskService


def build_runtime(config: AgentConfig | None = None) -> Runtime:
    config = config or AgentConfig()
    bus = EventBus(window=config.event_window)
    cancel_store = MemoryCancellationStore()
    registry = RunningTaskRegistry(cancel_store)
    service = TaskService(
        config=config, bus=bus, cancel_store=cancel_store, registry=registry,
    )
    return Runtime(
        config=config, bus=bus, cancel_store=cancel_store,
        registry=registry, service=service,
    )
