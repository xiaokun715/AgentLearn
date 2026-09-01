"""ReplayBuffer 抽象基类（设计说明书 §23 Replay Buffer）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from ..core.event import StreamEvent


class ResumeWindowExpired(Exception):
    """客户端请求的 last_seq 已超出 Replay Window（设计说明书 §25）。

    此时无法假装恢复，应返回 409 / resume_window_expired。
    reason="behind"：last_seq 早于窗口最旧 seq（已被淘汰，无法重放）。
    reason="ahead"：last_seq 超过服务端已产出的最大 seq（客户端声称超前，需重新请求）。
    """

    def __init__(
        self,
        stream_id: str,
        last_seq: int,
        *,
        oldest_seq: int = 0,
        newest_seq: int = 0,
        reason: str = "behind",
    ):
        super().__init__(
            f"stream {stream_id}: last_seq={last_seq} 不在窗口 "
            f"[{oldest_seq}, {newest_seq}] 内 (reason={reason})"
        )
        self.stream_id = stream_id
        self.last_seq = last_seq
        self.oldest_seq = oldest_seq
        self.newest_seq = newest_seq
        self.reason = reason


class ReplayBuffer(ABC):
    """保存最近 max_events 个事件，支持按 last_seq 重放。"""

    @abstractmethod
    async def append(self, event: StreamEvent) -> None:
        """追加一条事件（超出窗口时淘汰最旧）。"""

    @abstractmethod
    async def events(self) -> List[StreamEvent]:
        """返回全部已缓冲事件（按 seq 升序）。"""

    @abstractmethod
    async def replay(self, last_seq: int) -> List[StreamEvent]:
        """返回所有 seq > last_seq 的事件。"""

    @abstractmethod
    async def oldest_seq(self) -> int:
        """当前窗口内最旧的 seq；窗口为空时返回 0。"""

    @abstractmethod
    async def last_seq(self) -> int:
        """当前窗口内最新的 seq；窗口为空时返回 0。"""

    async def validate_replay_window(self, stream_id: str, last_seq: int) -> None:
        """校验 last_seq 是否仍在窗口内（last_seq<=0 视为从头开始，永远合法）。

        同时校验上下界：
          - 下界：last_seq 已被窗口淘汰（§25）-> 无法重放。
          - 上界：last_seq 超过服务端已产出的最大 seq -> 非法游标，
            若放行会让去重逻辑跳过所有事件导致连接永久挂起。
        """
        if last_seq <= 0:
            return
        oldest = await self.oldest_seq()
        newest = await self.last_seq()
        if last_seq < oldest:
            raise ResumeWindowExpired(
                stream_id, last_seq, oldest_seq=oldest, newest_seq=newest, reason="behind"
            )
        if last_seq > newest:
            raise ResumeWindowExpired(
                stream_id, last_seq, oldest_seq=oldest, newest_seq=newest, reason="ahead"
            )
