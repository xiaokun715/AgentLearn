"""ResearchAgent —— 演示用多步骤 Agent（对应第十章叙事：查账单/天气 + 抓网页 + 出报告）。

步骤设计成“前面快、中间慢、结尾写库/写文件”，正好能同时演示四道防线：

1. ``collect``    查账单 + 查天气（快工具，先攒下一批“事实”）
2. ``deep_dive``  慢速抓取网页（**长时间网络 I/O**：层2 拦不住、层3 能掐断）
3. ``finalize``   先把每条事实逐行写进“临时表/临时文件”（制造脏数据），
                  最后 commit + publish —— 取消发生在这一段时，兜底层要回滚。
"""
from __future__ import annotations

import asyncio

from ..domain.events import TaskEventType
from .base import AgentContext, BaseAgent
from .llm import MockLLM
from .tools import BillsTool, WeatherTool, WebFetchTool


class ResearchAgent(BaseAgent):
    name = "research_agent"
    description = "查账单/天气 -> 慢速抓网页 -> 生成报告（演示优雅中止）"
    steps = ["collect", "deep_dive", "finalize"]
    tools = {
        "fetch_bills": BillsTool(),
        "fetch_weather": WeatherTool(),
        "fetch_web": WebFetchTool(),
    }

    def __init__(self, llm: MockLLM | None = None) -> None:
        self.llm = llm or MockLLM()

    # ---- step 1: 快速收集事实 -------------------------------------------------
    async def _step_collect(self, task, ctx: AgentContext) -> dict | None:
        await ctx.llm(f"拆解用户诉求：{task.query}")
        await ctx.tool("fetch_bills", days=task.days, city=task.city)
        await ctx.tool("fetch_weather", city=task.city)
        return None

    # ---- step 2: 慢速网络 I/O（层3 的靶子）------------------------------------
    async def _step_deep_dive(self, task, ctx: AgentContext) -> dict | None:
        url = f"https://news.example.com/search?q={task.query}"
        res = await ctx.tool("fetch_web", url=url)
        # 只有网页完整下载完成，这条“事实”才会被记下 ——
        # 若被掐断在下载中途，Partial Yield 里自然不会有它（体现“剩余操作已取消”）。
        ctx.collect("web", f"[网页 {url}] {res['summary']}")
        await ctx.llm("基于抓到的网页内容提炼对账单/天气查询有关的关键信息")
        return None

    # ---- step 3: 写临时表/临时文件 + 收尾 -------------------------------------
    async def _step_finalize(self, task, ctx: AgentContext) -> dict | None:
        report = await ctx.llm("把全部事实整理成一份正式报告")

        # 往“数据库临时表”逐行写脏数据（每行之间有间隔 -> 取消可以落在中间）
        ctx.emit(TaskEventType.FINALIZE_STAGING, {"rows": len(task.facts)})
        for f in task.facts:
            ctx.db.stage({"task_id": task.task_id, "kind": f["kind"], "text": f["text"]})
            await asyncio.sleep(ctx.config.finalize_row_delay)

        # 往“临时文件”里逐行写
        lines = [f"# {task.query} 分析报告", report, ""]
        lines += [f"- {f['text']}" for f in task.facts]
        for line in lines:
            await asyncio.sleep(ctx.config.finalize_delay)
            ctx.ws.append_line(line)

        # 一切正常 -> 提交事务 + 发布报告（若取消发生在上面任意一处，都会被回滚）
        committed = ctx.db.commit()
        report_path = ctx.ws.publish()
        return {
            "report_file": report_path,
            "committed_rows": committed,
            "report": report,
        }
