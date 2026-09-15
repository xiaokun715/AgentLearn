"""第 1 层 · 工作记忆 (Working Memory) —— 模型的 Context Window。

对齐说明书：工作记忆 = 大模型当前推理步实际计算的 Token 窗口，容量狭窄且昂贵，
管理原则是**严格入模过滤 + 引用传递**。本类维护一个有预算的“帧”列表：

* ``add(kind, content)`` 追加一帧(profile/episodic/turn/tool/…)，超预算即丢最旧的非保护帧；
* ``assemble_context()`` 把帧拼成真正喂给模型的上下文串；
* ``protected`` 帧(如 system 约束)永远钉在顶部不被裁。

Token 用近似估计(约 2 个字符 ≈ 1 token)只是为了演示预算机制，不追求精确计费。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


def approx_tokens(text: str) -> int:
    """粗略估算 token 数：中英混排约 1 个汉字≈1 token、拉丁字符 4 个≈1 token。"""
    if not text:
        return 0
    han = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - han
    return max(1, math.ceil(han) + math.ceil(other / 4))


@dataclass
class Frame:
    """工作记忆里的一个片段(带来源标签，方便回溯它来自哪一层)。"""

    kind: str            # system / profile / episodic / turn / tool / thought
    content: str
    source: str = ""     # 原始来源：ProfileStore / EpisodicRecall / 工具返回 / 对话
    token_est: int = 0

    def __post_init__(self) -> None:
        self.token_est = self.token_est or approx_tokens(self.content)

    def render(self) -> str:
        return f"[{self.kind}] {self.content}" if self.kind != "system" else self.content


class WorkingMemory:
    """有预算的上下文窗口。超出 budget 时按“最旧先裁”淘汰非保护帧。"""

    PROTECTED = {"system", "profile"}  # 系统约束 + 用户画像(每次请求静态热加载)钉住不裁

    def __init__(self, budget_units: int = 600) -> None:
        self.budget = budget_units
        self.frames: list[Frame] = []
        self.trimmed: list[Frame] = []  # 记录被裁掉的帧(可观测)

    # ------------------------------------------------------------------
    def add(self, kind: str, content: str, source: str = "") -> Frame:
        """追加一帧并执行预算淘汰。返回新帧。"""
        frame = Frame(kind=kind, content=content, source=source)
        self.frames.append(frame)
        self._enforce_budget()
        return frame

    def clear(self) -> None:
        self.frames.clear()
        self.trimmed.clear()

    def used_units(self) -> int:
        return sum(f.token_est for f in self.frames)

    # ------------------------------------------------------------------
    def _enforce_budget(self) -> None:
        """超预算就从最旧的非保护帧开始裁。

        原则：system/profile 帧钉住不裁；且永远保留“最新那一帧”(刚产生的动作/工具返回不能丢，
        它是当前推理的产物) —— 真实工作记忆正是丢弃旧的、给新的腾地方。
        """
        # 有多少“可裁”的帧(非 protected)？
        def evictable() -> list[int]:
            return [
                i for i, f in enumerate(self.frames) if f.kind not in self.PROTECTED
            ]

        while self.used_units() > self.budget and len(evictable()) > 1:
            oldest = evictable()[0]  # 帧按时间序排列，最靠前 = 最旧
            evicted = self.frames.pop(oldest)
            self.trimmed.append(evicted)

    # ------------------------------------------------------------------
    def assemble_context(self, system_prompt: str = "") -> str:
        """把帧拼成模型可见的上下文(演示/API 用它看“模型实际看到什么”)。

        顺序：外部 system_prompt → 钉住的 system 帧 → 其余帧按加入次序；相邻重复行去重。
        """
        lines: list[str] = []
        if system_prompt:
            lines.append(system_prompt)
        lines.extend(f.render() for f in self.frames)
        # 用 dict.fromkeys 保持顺序下去重，避免同一画像行重复出现两次
        return "\n".join(dict.fromkeys(lines))

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """观测 API / demo 输出用。"""
        return {
            "budget": self.budget,
            "used_units": self.used_units(),
            "trimmed": [asdict(f) for f in self.trimmed],
            "frames": [asdict(f) for f in self.frames],
        }
