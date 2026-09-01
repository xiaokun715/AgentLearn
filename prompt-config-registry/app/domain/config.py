"""Agent Config 领域模型（设计说明书 §9~§11）。

Agent 实际运行不只有 Prompt，还包括 Model / Parameters / Tools / Guardrails。
``AgentConfig`` 是这些引用的一次性 **Snapshot** —— 记录"这次执行到底用了什么"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ModelConfig:
    """模型引用（§9）。"""

    provider: str = "qwen"
    name: str = "qwen3.5-27b"

    def to_dict(self) -> dict:
        return {"provider": self.provider, "name": self.name}

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        return cls(provider=data.get("provider", "qwen"), name=data.get("name", ""))


@dataclass(slots=True)
class GenerationParameters:
    """采样参数（§9）。"""

    temperature: float = 0.2
    top_p: float | None = None
    max_tokens: int | None = None

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"temperature": self.temperature}
        if self.top_p is not None:
            data["top_p"] = self.top_p
        if self.max_tokens is not None:
            data["max_tokens"] = self.max_tokens
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "GenerationParameters":
        return cls(
            temperature=float(data.get("temperature", 0.2)),
            top_p=data.get("top_p"),
            max_tokens=data.get("max_tokens"),
        )


@dataclass(slots=True)
class PromptRef:
    """Config 对某个 Prompt 版本的引用（§9）。"""

    name: str
    version: int

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version}

    @classmethod
    def from_dict(cls, data: dict) -> "PromptRef":
        return cls(name=data["name"], version=data["version"])


@dataclass(slots=True)
class AgentConfig:
    """一个**不可变**的 Agent Config 版本（相当于 SQL ``agent_configs`` 表的行）。

    config 里存的是"引用"而不是内容拷贝 —— 但 Resolver 会把它们展开成
    运行时可以直接使用的 Snapshot（§11 / §21）。
    """

    agent_name: str
    version: int
    model: ModelConfig = field(default_factory=ModelConfig)
    parameters: GenerationParameters = field(default_factory=GenerationParameters)
    prompt: PromptRef = field(default_factory=lambda: PromptRef("", 0))
    tools: dict[str, Any] = field(default_factory=lambda: {"version": 1})  # §9 tools.version
    guardrails: dict[str, Any] = field(default_factory=lambda: {"version": 1})
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {
            "agent": self.agent_name,
            "version": self.version,
            "model": self.model.to_dict(),
            "parameters": self.parameters.to_dict(),
            "prompt": self.prompt.to_dict(),
            "tools": self.tools,
            "guardrails": self.guardrails,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentConfig":
        return cls(
            agent_name=data["agent"],
            version=data["version"],
            model=ModelConfig.from_dict(data.get("model", {})),
            parameters=GenerationParameters.from_dict(data.get("parameters", {})),
            prompt=PromptRef.from_dict(data["prompt"]),
            tools=data.get("tools", {"version": 1}),
            guardrails=data.get("guardrails", {"version": 1}),
            created_by=data.get("created_by", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    def __str__(self) -> str:
        return f"{self.agent_name}:v{self.version}"


@dataclass(slots=True)
class ResolvedSnapshot:
    """Resolver 输出的运行时配置快照（设计说明书 §11 / §31）。

    这正是 Agent Runtime 直接消费的对象 —— 不再需要任何硬编码。
    """

    agent: str
    config_version: int
    prompt: dict[str, Any]          # {"name", "version", "template", "variables", "metadata"}
    model: dict[str, Any]           # {"provider", "name"}
    parameters: dict[str, Any]      # {"temperature", "max_tokens", ...}
    tools: dict[str, Any]           # {"version": 8, ...}
    guardrails: dict[str, Any]      # {"version": 3}
    routing: dict[str, Any] = field(default_factory=dict)  # {"experiment", "variant", "bucket", "rules"}

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "config_version": self.config_version,
            "prompt": self.prompt,
            "model": self.model,
            "parameters": self.parameters,
            "tools": self.tools,
            "guardrails": self.guardrails,
            "routing": self.routing,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ResolvedSnapshot":
        return cls(
            agent=data["agent"],
            config_version=data["config_version"],
            prompt=data["prompt"],
            model=data["model"],
            parameters=data["parameters"],
            tools=data["tools"],
            guardrails=data["guardrails"],
            routing=data.get("routing", {}),
        )

    def execution_identity(self) -> str:
        """执行身份（§34 / §35）：写进 Trace / Token Meter / Semantic Cache Key。"""
        return "|".join(
            [
                self.agent,
                f"config:v{self.config_version}",
                f"prompt:{self.prompt.get('name')}:v{self.prompt.get('version')}",
                f"model:{self.model.get('name')}",
                f"tools:v{self.tools.get('version')}",
                f"exp:{self.routing.get('experiment', '-')}:{self.routing.get('variant', '-')}",
            ]
        )
