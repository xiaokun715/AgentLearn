"""缓存 Key 约定（设计说明书 §23）。

两类 key：
1. ``deploy:{agent}:{environment}``  —— Deployment 路由表（**可变**）
   → 发布 / 灰度 / 回滚时必须显式失效（Cache Invalidation）。

2. ``config:{agent}:v{version}``    —— 解析出的 Config Snapshot（**版本化 key**）
   → 因为 Version 不可变，同一 key 的内容永远一致；
     发布 v13 时 v12 的缓存无需清理（Versioned Cache Key，§23）。

版本化 key 的关键收益：新版本上线不会把旧版本的缓存"顶掉"，
缓存命中率更高，且不可能出现「新 Prompt + 旧答案」。
"""
from __future__ import annotations

DEPLOYMENT_KEY = "deploy:{agent}:{environment}"
SNAPSHOT_KEY = "config:{agent}:v{version}"


def deployment_key(agent: str, environment: str) -> str:
    return DEPLOYMENT_KEY.format(agent=agent, environment=environment)


def snapshot_key(agent: str, version: int) -> str:
    return SNAPSHOT_KEY.format(agent=agent, version=version)
