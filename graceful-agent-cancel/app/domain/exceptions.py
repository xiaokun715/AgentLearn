"""领域异常。"""
from __future__ import annotations


class TaskNotFoundError(Exception):
    """任务不存在。"""


class UnknownAgentError(Exception):
    """请求的 agent 不存在。"""


class TaskNotRunningError(Exception):
    """任务已到达终态，无法再取消。"""
