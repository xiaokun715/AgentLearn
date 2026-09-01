"""异常定义。

按设计说明书 §24 拆分可重试 / 不可重试异常；另定义 WorkerCrash（模拟
Worker 进程崩溃，不进入重试计数，而是等待 Lease 过期后被 Reaper 接管）
与 CancellationRequested（协作式取消信号）。
"""
from __future__ import annotations


class RetryableError(Exception):
    """可重试的瞬时错误（LLM timeout / HTTP 5xx / rate limit）。"""


class NonRetryableError(Exception):
    """不可重试的错误（无效 Prompt / 权限不足 / 数据校验失败）。"""


class WorkerCrash(Exception):
    """模拟 Worker 崩溃：任务被中断但不判失败，交给 Reaper + Lease 恢复（§41）。"""


class CancellationRequested(Exception):
    """收到取消信号，停止当前执行（§44）。"""
