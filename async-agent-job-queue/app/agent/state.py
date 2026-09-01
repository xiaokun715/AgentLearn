"""AgentState —— Agent 图的执行状态（设计说明书 §18 / §20）。

一个 Job 内可被持久化为 Checkpoint 的最小状态单元：
completed_steps（已完成的步骤）+ tool_records（Tool 执行记录，幂等依据）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.job import Job


@dataclass
class AgentState:
    job_id: str
    query: str
    current_step: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    tool_records: dict[str, dict] = field(default_factory=dict)

    # ResearchAgent 专用字段（chaos_agent 复用同一状态结构）
    analysis: str = ""
    search_result: list = field(default_factory=list)
    insights: str = ""
    report: str = ""
    finished: bool = False

    # ---- 执行流 -----------------------------------------------------------

    def next_step(self, steps: list[str]) -> str | None:
        for s in steps:
            if s not in self.completed_steps:
                return s
        return None

    def apply(self, step: str, result: dict, steps: list[str]) -> None:
        """把某个 step 的返回值并入状态，并把该 step 标记为已完成。"""
        self.current_step = step
        for key, value in result.items():
            if hasattr(self, key) and key not in self._IMMUTABLE_FIELDS:
                setattr(self, key, value)
        if step not in self.completed_steps:
            self.completed_steps.append(step)
        if self.completed_steps[-1:] == steps[-1:]:
            self.finished = True

    _IMMUTABLE_FIELDS = frozenset(
        {"job_id", "query", "completed_steps", "tool_records", "finished", "current_step"}
    )

    def progress(self, steps: list[str]) -> int:
        if not steps:
            return 0
        return int(len(self.completed_steps) / len(steps) * 100)

    @property
    def result(self) -> dict:
        return {"report": self.report}

    # ---- Checkpoint 序列化（§18） -----------------------------------------

    def to_checkpoint(self) -> dict:
        return {
            "job_id": self.job_id,
            "step": self.current_step,
            "completed_steps": list(self.completed_steps),
            "tool_records": self.tool_records,
            "state": {
                "query": self.query,
                "analysis": self.analysis,
                "search_result": self.search_result,
                "insights": self.insights,
                "report": self.report,
            },
        }

    @classmethod
    def initial(cls, job: Job) -> "AgentState":
        query = str(job.input.get("query", ""))
        return cls(job_id=job.id, query=query)

    @classmethod
    def from_checkpoint(cls, job: Job, cp: dict | None) -> "AgentState":
        if cp is None:
            return cls.initial(job)
        st = cp.get("state", {})
        return cls(
            job_id=job.id,
            query=str(st.get("query", job.input.get("query", ""))),
            current_step=cp.get("step"),
            completed_steps=list(cp.get("completed_steps", [])),
            tool_records=dict(cp.get("tool_records", {})),
            analysis=str(st.get("analysis", "")),
            search_result=list(st.get("search_result", [])),
            insights=str(st.get("insights", "")),
            report=str(st.get("report", "")),
            finished=bool(cp.get("finished", False)),
        )
