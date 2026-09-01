"""Invalidation Manager（设计说明书 §24 ~ §25）。

除了 TTL 自动过期，还支持主动失效：
  - 按租户 / 命名空间 / 模型 全量失效（§24）
  - 按知识库版本失效（§25）：v42 -> v43 时只删旧版本条目，
    不用遍历删除所有缓存；未命中的版本直接 MISS。
"""
from __future__ import annotations

from typing import Any

from ..storage.base import CacheStore


class InvalidationManager:
    def __init__(self, store: CacheStore):
        self._store = store

    async def invalidate(
        self,
        *,
        cache_id: str | None = None,
        namespace: str | None = None,
        tenant_id: str | None = None,
        model: str | None = None,
        knowledge_version: str | None = None,
        agent_type: str | None = None,
        task_type: str | None = None,
    ) -> int:
        """主动失效（§24）。返回删除条数。"""
        if cache_id is not None:
            await self._store.delete(cache_id)
            return 1
        return await self._store.delete_many(
            namespace=namespace,
            tenant_id=tenant_id,
            model=model,
            knowledge_version=knowledge_version,
            agent_type=agent_type,
            task_type=task_type,
        )

    async def invalidate_by_version(
        self,
        knowledge_version: str,
        *,
        namespace: str | None = None,
        tenant_id: str | None = None,
    ) -> int:
        """按知识库版本失效（§25）：只删旧版本条目，避免全表遍历。"""
        return await self._store.delete_many(
            namespace=namespace,
            tenant_id=tenant_id,
            knowledge_version=knowledge_version,
        )
