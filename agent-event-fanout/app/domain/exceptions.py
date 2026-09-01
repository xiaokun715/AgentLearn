"""领域异常 —— 统一映射为 HTTP 错误（设计说明书 §31 API）。"""
from __future__ import annotations


class EventFanoutError(Exception):
    """领域错误基类。"""


class NotFoundError(EventFanoutError):
    """资源不存在。"""


class ConflictError(EventFanoutError):
    """唯一键冲突 / 状态非法迁移。"""


class InvalidStateError(EventFanoutError):
    """Delivery 状态机非法迁移。"""


class EventTypeError(EventFanoutError):
    """Event Type 不合法。"""


class SubscriberError(EventFanoutError):
    """Subscriber 相关错误。"""
