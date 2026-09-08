"""运行配置。

所有“慢”都做成可配置的时延（Latency）—— 方便在 example / 测试里调到很小，
让四道防线能用几秒钟完整演示，而不是真的等一个 20 秒的网络 I/O。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass
class AgentConfig:
    # ---- 模拟时延（单位：秒）--------------------------------------------------
    llm_chunk_delay: float = 0.10       # 假 LLM 每“吐一个字块”间隔
    llm_chunks_per_think: int = 3       # 一次思考输出多少字块
    quick_tool_delay: float = 0.20      # 快工具（账单/天气）延迟
    web_fetch_total: float = 3.5        # 慢速“爬网页”总时长（层3 必须足够长，方便人工按停）
    web_fetch_chunk: float = 0.35       # 爬取过程中每个分片间隔（用于 TOOL_CHUNK 事件）
    finalize_row_delay: float = 0.06    # 落库时每写一行临时表/临时文件之间的间隔
    finalize_delay: float = 0.06        # 收尾阶段其它小步进

    # ---- 取消语义 -------------------------------------------------------------
    # "force"        强制取消：标记 cancelled + asyncio.Task.cancel()（层2 与层3 一起）
    # "cooperative"  协作式取消：只标记 cancelled，Agent 在下一个动作边界跳出（仅层2）
    default_cancel_mode: str = "force"

    # ---- 事件/SSE -------------------------------------------------------------
    event_window: int = 500             # 每个 task 在内存中保留的历史事件条数

    @classmethod
    def from_env(cls) -> "AgentConfig":
        return AgentConfig(
            llm_chunk_delay=float(os.getenv("CANCEL_LLM_CHUNK_DELAY", "0.10")),
            web_fetch_total=float(os.getenv("CANCEL_WEB_FETCH_TOTAL", "3.5")),
            default_cancel_mode=os.getenv("CANCEL_DEFAULT_MODE", "force"),
        )

    def fast(self) -> "AgentConfig":
        """给 example / 测试用的“飞快”配置，几百毫秒跑完整条链路。"""
        return replace(
            self,
            llm_chunk_delay=0.002,
            llm_chunks_per_think=2,
            quick_tool_delay=0.005,
            web_fetch_total=0.6,
            web_fetch_chunk=0.03,
            finalize_row_delay=0.005,
            finalize_delay=0.005,
        )
