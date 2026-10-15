"""工具层基础设施 —— 诊断工具的统一契约。

说明书 §10.1 把 Tool 定位为「执行真实的数据查询、日志读取、环境检查等操作」。
本模块给出三样东西：

- :class:`ToolContext`：工具能触达的世界（模拟平台 + 时钟 + 调用记录）；
- :class:`ToolError`：工具侧的失败（**与「检查发现异常」是两回事**，
  后者要变成 ``Observation.ANOMALY``，前者要变成 ``Observation.ERROR``）；
- :class:`ToolRegistry`：名字 → 实现 的注册表，执行器据此把计划里的
  ``skill`` 名字解析成真正的调用。

**这个「异常 vs 异常发现」的区分是本 Demo 里最容易被忽略、也最容易做错的一点。**
工具报错（超时、接口 500）不构成任何根因假设的证据，把它当成 ANOMALY 会让系统
把「查不到」误读成「有问题」。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..clock import Clock
from ..domain.enums import Observation
from ..domain.models import PlanNode, ToolObservation
from ..sim import SimulatedPlatform


class ToolError(Exception):
    """工具自身的执行失败（超时、依赖不可用、返回码非零）。

    对应 :attr:`~app.domain.enums.Observation.ERROR`：它说明**这次检查没做成**，
    而不是「检查发现有问题」。
    """


@dataclass
class ToolCallRecord:
    """一次工具调用的审计记录。"""

    skill: str
    outcome: str
    summary: str
    duration_ms: int = 0
    error: str | None = None


@dataclass
class ToolContext:
    """工具运行时上下文。"""

    platform: SimulatedPlatform
    clock: Clock
    records: list[ToolCallRecord] = field(default_factory=list)
    #: 工具见过的原始证据文本 → ``artifact://`` 引用（对应真机的对象存储）
    artifacts: dict[str, str] = field(default_factory=dict)

    def record(self, call: ToolCallRecord) -> None:
        self.records.append(call)

    def store_artifact(self, name: str, content: str) -> str:
        ref = f"artifact://{name}"
        self.artifacts[ref] = content
        return ref


ToolFn = Callable[[ToolContext, PlanNode], ToolObservation]


class ToolRegistry:
    """``skill name -> 实现`` 的注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolFn] = {}

    def register(self, name: str) -> Callable[[ToolFn], ToolFn]:
        def decorator(fn: ToolFn) -> ToolFn:
            self._tools[name] = fn
            return fn

        return decorator

    def add(self, name: str, fn: ToolFn) -> None:
        self._tools[name] = fn

    def resolve(self, name: str) -> ToolFn:
        fn = self._tools.get(name)
        if fn is None:
            raise KeyError(f"没有注册诊断工具：{name}")
        return fn

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)


def observation_outcome(has_findings: bool, sparse: bool = False) -> Observation:
    """把「有没有发现项」翻译成观测结果。

    集中在这里做，是为了让所有工具用**同一套**口径判断 ANOMALY / NORMAL ——
    否则「有的工具空列表算正常、有的算无结论」，规划器的似然表就失去意义了。
    """
    if sparse:
        return Observation.INCONCLUSIVE
    return Observation.ANOMALY if has_findings else Observation.NORMAL
