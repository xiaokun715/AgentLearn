"""动态 Workflow 路由系统的枚举。

对齐《第十七章：动态 Workflow 与 Agent 路由设计说明书》：
- 二、§2   三个必须分开的概念       -> :class:`RouteTarget`
- 四、§4.4 池子健康度               -> :class:`AgentHealth`
- 五、§5.3 意图置信度低要澄清        -> :class:`IntentStatus`
- 七、§7   执行模式                 -> :class:`ExecutionMode`
- 十一、§11 模板标签                -> :class:`TemplateSource` / :class:`TemplateState`
- 十二、§12 保存与丢弃              -> :class:`TemplateFate`
"""
from __future__ import annotations

from enum import Enum


class RouteTarget(str, Enum):
    """§2 路由引擎能给出的**五种**终点。

    很多实现只有「直连」和「走 workflow」两种，于是所有「不确定」都被
    硬塞进其中之一。这里刻意把 ``CLARIFY`` 和 ``REJECT`` 提成一等公民：

    * 说不清就**问**，比硬路由到错误的子 Agent 便宜得多；
    * 说不对就**拒**，比让一个不匹配的 Agent 编一段看起来合理的回答便宜得多。
    """

    DIRECT_AGENT = "DIRECT_AGENT"          # 单个子 Agent 就能回答
    EXISTING_TEMPLATE = "EXISTING_TEMPLATE"  # 命中已有模板
    COMPOSE_NEW = "COMPOSE_NEW"            # 现场生成一份新模板
    CLARIFY = "CLARIFY"                    # 意图不清，先澄清
    REJECT = "REJECT"                      # 超出能力边界


class IntentStatus(str, Enum):
    """§5.3 意图识别的结论质量。

    与第十五章的 ``FailureCategory.UNKNOWN`` 同一条思路：
    **「没识别出来」是一个合法结论，不是失败**。
    把它伪装成一个低置信度的猜测，后面所有环节都会跟着错。
    """

    RESOLVED = "RESOLVED"        # 明确识别
    AMBIGUOUS = "AMBIGUOUS"      # 有候选但分不开 -> 澄清
    UNKNOWN = "UNKNOWN"          # 完全没头绪 -> 澄清或拒绝


class ExecutionMode(str, Enum):
    """§7 用户可以显式指定的执行模式。"""

    AUTO = "AUTO"                    # 交给路由引擎
    FORCE_DIRECT = "FORCE_DIRECT"    # 强制直连（对照组）
    FORCE_WORKFLOW = "FORCE_WORKFLOW"  # **强制先生成模板，再按模板跑**（需求 3）


class TemplateSource(str, Enum):
    """§11.1 模板从哪来 —— 这是标签里最能影响信任度的一个字段。"""

    SEED = "SEED"                  # 平台预置
    LLM_GENERATED = "LLM_GENERATED"  # 现场由模型生成
    MANUAL = "MANUAL"              # 人工编写
    PROMOTED = "PROMOTED"          # 由一次成功的直连轨迹提升而来


class TemplateState(str, Enum):
    """§13 模板库的生命周期状态。"""

    DRAFT = "DRAFT"          # 刚生成，还没被验证过
    ACTIVE = "ACTIVE"        # 可用
    DEPRECATED = "DEPRECATED"  # 被新版本取代，保留但不再首选
    BROKEN = "BROKEN"        # 引用的子 Agent 已下线 -> 不可执行


class TemplateFate(str, Enum):
    """§12 一次运行之后，生成出来的模板怎么处置（需求 3 的「保存或丢弃」）。"""

    SAVE = "SAVE"
    DISCARD = "DISCARD"
    PENDING = "PENDING"      # 等用户决定 —— 默认不落库
    #: 这次运行根本没有产生新模板（直连 / 复用已有模板）
    NONE = "NONE"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def order(self) -> int:
        return {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[self.value]


class StepStatus(str, Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"


class AgentHealth(str, Enum):
    """§4.4 池子里的子 Agent 会下线、会降级 —— 路由必须知道。"""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"    # 可用但变慢/变贵，评分要打折
    OFFLINE = "OFFLINE"      # 不可用，路由必须绕开
