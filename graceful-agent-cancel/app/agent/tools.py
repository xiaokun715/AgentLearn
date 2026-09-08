"""Agent 用到的外部工具（全部是可取消的异步协程）。

- ``fetch_bills`` / ``fetch_weather``：快工具，对应“查账单/天气”；
- ``fetch_web``：**慢速网络 I/O**，内部分成很多个小 await 一块一块“下载”。
  这是层3 展示的重点 —— Agent 卡在它里面时，协作式取消等不到它结束，
  只有 ``asyncio.Task.cancel()`` 能当场把它掐断。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..domain.events import TaskEventType

if TYPE_CHECKING:
    from .base import AgentContext


class BillsTool:
    name = "fetch_bills"

    async def run(self, ctx: "AgentContext", days: int = 2, city: str = "上海") -> dict:
        await asyncio.sleep(ctx.config.quick_tool_delay)
        rows = []
        for i in range(days):
            rows.append({
                "date": f"2026-09-0{i + 1}",
                "amount": 128 + i * 66,
                "desc": f"{city} 便利店消费",
                "currency": "CNY",
            })
        lines = [
            f"[账单 {r['date']}] {r['desc']} 支出 ¥{r['amount']}"
            for r in rows
        ]
        for line in lines:
            ctx.collect("bill", line)
        return {"rows": len(rows), "summary": f"已查询到 {len(rows)} 条账单", "lines": lines}


class WeatherTool:
    name = "fetch_weather"

    async def run(self, ctx: "AgentContext", city: str = "上海") -> dict:
        await asyncio.sleep(ctx.config.quick_tool_delay)
        text = f"[天气 {city}] 晴 27℃ 微风，适合出行"
        ctx.collect("weather", text)
        return {"summary": text}


class WebFetchTool:
    """慢速“爬网页”。把下载切成 N 个分片，每片之间 sleep —— 可被层3 掐断。"""

    name = "fetch_web"

    async def run(self, ctx: "AgentContext", url: str = "https://example.com/report") -> dict:
        cfg = ctx.config
        total = max(cfg.web_fetch_total, cfg.web_fetch_chunk)
        n = max(1, int(round(total / cfg.web_fetch_chunk)))
        buf = ""
        for i in range(n):
            await asyncio.sleep(cfg.web_fetch_chunk)          # 掐断点
            buf += f"[第{i + 1}/{n}块网页正文]" + "数据片段" * 6
            ctx.emit(TaskEventType.TOOL_CHUNK, {
                "tool": "fetch_web", "chunk_index": i + 1, "total": n,
                "buffered_chars": len(buf),
            })
        return {
            "url": url,
            "bytes": len(buf),
            "snippet": buf[:60],
            "summary": f"已下载 {n} 个分片共 {len(buf)} 字符",
        }
