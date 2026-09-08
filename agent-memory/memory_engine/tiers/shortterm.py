"""第 2 层 · 短期/会话记忆 (Short-Term Memory) —— Redis Session / Checkpointer。

对齐说明书 §短期与会话记忆 的三个机制：

1. **Checkpointer(状态快照)** —— ``checkpoint()/restore()``：会话中间态可序列化为 dict，
   服务崩溃重启后可恢复断点；
2. **动态折叠(In-flight Pruning)** —— ``fold()``：对话超过窗口后，把最旧若干轮交给
   summarizer 折叠成一段简短摘要，释放上下文空间(生产里这是轻量模型干的活，
   这里默认用启发式取“信号句”，可通过 ``summarizer`` 换成一个真 LLM 回调)；
3. **会话结束即退火** —— 上下文历史只活在本次会话，任务终态后由后台异步蒸馏转存 Episodic。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

from ..records import utcnow

_SIGNAL = re.compile(r"(报错|错误|失败|成功|告警|解决|修复|排查|还原|可用)", re.UNICODE)


@dataclass
class Turn:
    """一轮对话/一次 Agent 动作的原始记录。"""

    role: str            # user / assistant / tool / system
    content: str
    ts: datetime = field(default_factory=utcnow)

    def render(self) -> str:
        return f"{self.role}: {self.content}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d


Summarizer = Callable[[Sequence[Turn]], str]


def heuristic_summarize(turns: Sequence[Turn], max_lines: int = 3) -> str:
    """默认“伪折叠”：从被折叠的轮次里抽出含信号词的句子，截断成要点。

    真实产品把该回调换成 LLM distill；这里保持确定性、无网络即可演示“折叠”机制。
    """
    hits = [t.content.strip() for t in turns if _SIGNAL.search(t.content)]
    kept = hits[:max_lines]
    if not kept:
        kept = [t.content.strip()[:40] for t in turns][:1]
    return "；".join(kept)[:200]


class ShortTermMemory:
    """单次会话状态机容器：原始轮次 + 滚动折叠摘要 + 任务 scratchpad。"""

    def __init__(
        self,
        window: int = 6,
        keep_recent: int = 4,
        summarizer: Summarizer = heuristic_summarize,
    ) -> None:
        self.window = window
        self.keep_recent = keep_recent
        self.summarizer = summarizer
        self.turns: list[Turn] = []
        self.summary: str = ""               # 早期轮次折叠出的滚动摘要
        self.scratchpad: dict[str, Any] = {}  # 任务中间规划状态(Session 内临时)

    # ------------------------------------------------------------------
    def append(self, role: str, content: str) -> Turn:
        """记录一轮并触发动态折叠(超过窗口即压缩最旧的几轮)。"""
        turn = Turn(role=role, content=content)
        self.turns.append(turn)
        self._fold_if_needed()
        return turn

    def _fold_if_needed(self) -> None:
        while len(self.turns) > self.window:
            # 保留最近 keep_recent 轮原样，其余全部交给摘要器
            overflow = self.turns[: len(self.turns) - self.keep_recent]
            del self.turns[: len(overflow)]
            new_bit = self.summarizer(overflow)
            if new_bit:
                self.summary = f"{self.summary}；{new_bit}".strip("；")

    def note(self, key: str, value: Any) -> None:
        """写 scratchpad(任务中间状态，等价往 Redis 写规划状态)。"""
        self.scratchpad[key] = value

    # ------------------------------------------------------------------
    def checkpoint(self) -> dict[str, Any]:
        """Checkpointer 快照：可序列化的会话状态(崩溃重启可恢复)。"""
        return {
            "summary": self.summary,
            "scratchpad": dict(self.scratchpad),
            "turns": [t.to_dict() for t in self.turns],
        }

    def restore(self, snap: dict[str, Any]) -> None:
        """从 ``checkpoint()`` 产物恢复会话断点。"""
        self.summary = snap.get("summary", "")
        self.scratchpad = dict(snap.get("scratchpad", {}))
        self.turns = [Turn(role=t["role"], content=t["content"]) for t in snap.get("turns", [])]

    def context_lines(self) -> list[str]:
        """会话当前能呈现给工作记忆的全部内容：折叠摘要 + 最近几轮原样。"""
        lines = []
        if self.summary:
            lines.append(f"[会话折叠摘要] {self.summary}")
        lines.extend(t.render() for t in self.turns)
        return lines

    def snapshot(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "turns_count": len(self.turns),
            "turns": [t.to_dict() for t in self.turns],
            "summary": self.summary,
            "scratchpad": dict(self.scratchpad),
        }
