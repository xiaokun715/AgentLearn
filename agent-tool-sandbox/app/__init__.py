"""Agent Tool Sandbox —— Agent 代码执行沙箱（设计说明书 §1-3）。

核心安全原则：Never trust model-generated code.
LLM 输出一律视为 UNTRUSTED INPUT，执行必须经过 Policy → Sandbox，而不是直接 exec。
"""

__version__ = "0.1.0"
