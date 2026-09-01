"""策略层（设计说明书 §15 ~ §16 / §35 ~ §36）。

- ThresholdPolicy：相似度阈值 —— 决定 Top-K 候选里谁能真正 HIT
- CachePolicy    ：Cacheability —— 决定什么请求/回答「不能」写缓存
"""
from __future__ import annotations

import re
from typing import Pattern

from ..core.entry import CacheResult, ChatRequest, SearchResult


class ThresholdPolicy:
    """相似度阈值策略（§15）。

    不要天真地认为 similarity >= 0.9 就永远 HIT（§14 / §17）：
    阈值必须通过数据集评估得到 Operating Point（§16）。
    本项目默认为 Mock 向量校准，接入真实模型必须重新标定。
    """

    def __init__(self, threshold: float = 0.83):
        self.threshold = float(threshold)

    def should_hit(self, similarity: float) -> bool:
        return similarity >= self.threshold

    def select(self, results: list[SearchResult], *, request: ChatRequest | None = None) -> SearchResult | None:
        """从 Top-K 候选中选出最相似且超过阈值的那一个（§13 / §49 第 5 步）。

        返回 None 表示 MISS。``request`` 目前只用于可读性/日志，策略本身不依赖。
        """
        if not results:
            return None
        best = max(results, key=lambda r: r.similarity)
        if self.should_hit(best.similarity):
            return best
        return None

    def to_dict(self) -> dict[str, float]:
        return {"threshold": self.threshold}


class CachePolicy:
    """Cacheability Policy（§36）：判断请求/回答是否允许写入缓存。

    哪些答案不能缓存（§35）：
      - 实时性问题（现在几点 / 天气 / 订单状态）—— 缓存会返回过期信息
      - 破坏性指令（执行删除数据库）—— 绝不能用语义缓存命中旧答案
      - temperature 过高 —— 生成结果天然随机，缓存命中只会放大随机性
      - 携带 tools —— Tool Call 参数不同则答案不同（§52 测试矩阵 Tool 行）
    """

    def __init__(
        self,
        *,
        max_temperature: float = 0.7,
        cache_with_tools: bool = False,
        cache_time_sensitive: bool = False,
        blocklist: list[str] | None = None,
    ):
        self.max_temperature = max_temperature
        self.cache_with_tools = cache_with_tools
        self.cache_time_sensitive = cache_time_sensitive
        # 非缓存意图的启发式关键词（§35）。命中即拒绝缓存。
        self.blocklist: list[Pattern[str]] = [
            re.compile(p) for p in (blocklist or [
                r"现在.*几点", r"当前时间",
                r"天气",
                r"订单状态", r"物流状态",
                r"帮我查询", r"帮我查一下",
                r"执行删除", r"删除数据库", r"drop\s+table",
            ])
        ]

    def is_cacheable(self, request: ChatRequest, response: dict | None = None) -> tuple[bool, str]:
        """返回 ``(是否可缓存, 原因)``。原因是空串表示可缓存。"""
        if request.temperature > self.max_temperature:
            return False, "temperature_high"

        if request.has_tools and not self.cache_with_tools:
            return False, "has_tools"

        if request.time_sensitive and not self.cache_time_sensitive:
            return False, "time_sensitive"

        for pattern in self.blocklist:
            if pattern.search(request.user_text):
                return False, f"blocklist:{pattern.pattern}"

        if response is None:
            return True, ""

        # 回答侧兜底：如果 LLM 自己声明了不可缓存标记（如流式里的 error），也不缓存
        meta = response.get("_meta") if isinstance(response, dict) else None
        if isinstance(meta, dict) and meta.get("cacheable") is False:
            return False, "response_not_cacheable"

        return True, ""

    def to_dict(self) -> dict[str, object]:
        return {
            "max_temperature": self.max_temperature,
            "cache_with_tools": self.cache_with_tools,
            "cache_time_sensitive": self.cache_time_sensitive,
            "blocklist": [p.pattern for p in self.blocklist],
        }
