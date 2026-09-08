"""CancellationStore —— 取消状态存取接口。

对应第十章 2.2 节的“分布式缓存里把这个 task_id 的状态强行标记为 status: cancelled”。
生产用 Redis；本 Demo 用进程内 dict 模拟，接口完全一致，方便以后替换。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class CancellationStore(ABC):
    @abstractmethod
    def is_cancelled(self, task_id: str) -> bool:
        """Agent Loop 每次核心动作前都会来查一次（层2 埋点）。"""

    @abstractmethod
    def mark_cancelled(self, task_id: str) -> None:
        """网关收到停止指令后，把该 task 标记为 cancelled（层1 -> 层2）。"""
