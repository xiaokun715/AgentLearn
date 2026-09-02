"""guardrails-middleware —— AI Security Middleware Demo。

一个独立于 LLM / Agent / Tool 的安全中间件，为 AI Application / Agent Runtime
提供统一的 **输入检测、Tool 安全、敏感信息保护、策略决策与安全审计** 能力。

核心思想（设计说明书 §02）：
    Prompt 告诉模型应该怎么做，Guardrails 决定系统允许它做什么。
"""
__version__ = "0.1.0"
