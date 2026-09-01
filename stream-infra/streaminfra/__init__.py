"""StreamInfra —— 简化版 LLM 流式响应基础设施。

设计说明书：《第三章：LLM 流式响应基础设施设计说明书.md》
核心：SSE / WebSocket 传输 + StreamEvent 事件模型 + 有界队列背压 +
     断线取消上游 + Last-Event-ID 重连重放 + TTFT/TPOT 指标。
"""

__version__ = "0.1.0"
