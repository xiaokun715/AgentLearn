"""领域异常 —— API 层据此映射 HTTP 状态码。"""
from __future__ import annotations


class ConfigRegistryError(Exception):
    """领域层根异常。"""


class NotFoundError(ConfigRegistryError):
    """资源不存在（Prompt / Config Version / Deployment）。"""


class ConflictError(ConfigRegistryError):
    """资源冲突（名字已存在 / 版本已存在）。"""


class ImmutableVersionError(ConflictError):
    """尝试修改不可变版本 —— 版本只能 append，不能 update（§8）。"""


class DeploymentError(ConfigRegistryError):
    """部署流程非法（状态机 / 流量范围 / 缺候选版本等）。"""
