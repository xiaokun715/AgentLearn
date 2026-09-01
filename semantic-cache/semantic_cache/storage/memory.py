"""MemoryStore（设计说明书 §27）。

纯内存实现，方便跑通全链路与测试。生产环境升级为 PostgreSQL + pgvector（§28）。
行为契约与 PostgresStore 完全一致：exact get / insert / 带元数据过滤的 Top-K 搜索 /
批量删除（TTL 过期条目在访问时惰性删除并计数）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from ..core.entry import CacheEntry, SearchResult
from ..search.base import cosine_similarity
from .base import CacheStore


class MemoryStore(CacheStore):
    def __init__(self, *, on_expired: Callable[[int], None] | None = None):
        self._entries: dict[str, CacheEntry] = {}
        # (namespace, tenant_id, model, fingerprint) -> cache_id，供精确命中 O(1) 查找
        self._exact_index: dict[tuple[str, str, str, str], str] = {}
        self._lock = asyncio.Lock()
        self._on_expired = on_expired

    # ---- 精确缓存 ------------------------------------------------------

    async def get_exact(self, *, namespace: str, tenant_id: str, model: str, fingerprint: str) -> CacheEntry | None:
        key = (namespace, tenant_id, model, fingerprint)
        async with self._lock:
            cache_id = self._exact_index.get(key)
            if cache_id is None:
                return None
            entry = self._entries.get(cache_id)
            if entry is None:
                return None
            if entry.expired:
                self._drop(cache_id, key=key)
                self._notify_expired(1)
                return None
            return entry

    # ---- 写入 ----------------------------------------------------------

    async def insert(self, entry: CacheEntry) -> None:
        async with self._lock:
            key = (entry.namespace, entry.tenant_id, entry.model, entry.fingerprint)
            old_id = self._exact_index.get(key)
            if old_id is not None and old_id != entry.cache_id:
                self._entries.pop(old_id, None)
            self._entries[entry.cache_id] = entry
            self._exact_index[key] = entry.cache_id

    # ---- 向量检索（§31：先过滤租户/模型/TTL，再按相似度排序）------------

    async def search(
        self,
        query_vector: list[float],
        *,
        namespace: str,
        tenant_id: str,
        model: str,
        top_k: int,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        to_drop: list[str] = []
        async with self._lock:
            for cache_id, entry in self._entries.items():
                if entry.namespace != namespace or entry.tenant_id != tenant_id or entry.model != model:
                    continue
                if entry.expired:
                    to_drop.append(cache_id)
                    continue
                sim = cosine_similarity(query_vector, entry.embedding)
                results.append(SearchResult(entry=entry, similarity=sim))

            for cache_id in to_drop:
                self._drop(cache_id)
            if to_drop:
                self._notify_expired(len(to_drop))

        results.sort(key=lambda r: r.similarity, reverse=True)
        return results[:top_k]

    # ---- 删除 / 失效 ---------------------------------------------------

    async def delete(self, cache_id: str) -> None:
        async with self._lock:
            self._drop(cache_id)

    async def delete_many(
        self,
        *,
        namespace: str | None = None,
        tenant_id: str | None = None,
        model: str | None = None,
        knowledge_version: str | None = None,
        agent_type: str | None = None,
        task_type: str | None = None,
    ) -> int:
        deleted = 0
        async with self._lock:
            for cache_id, entry in list(self._entries.items()):
                if namespace is not None and entry.namespace != namespace:
                    continue
                if tenant_id is not None and entry.tenant_id != tenant_id:
                    continue
                if model is not None and entry.model != model:
                    continue
                if knowledge_version is not None and entry.knowledge_version != knowledge_version:
                    continue
                if agent_type is not None and entry.agent_type != agent_type:
                    continue
                if task_type is not None and entry.task_type != task_type:
                    continue
                self._drop(cache_id)
                deleted += 1
        return deleted

    async def count(self, *, namespace: str | None = None, tenant_id: str | None = None) -> int:
        async with self._lock:
            n = 0
            for entry in self._entries.values():
                if namespace is not None and entry.namespace != namespace:
                    continue
                if tenant_id is not None and entry.tenant_id != tenant_id:
                    continue
                n += 1
            return n

    # ---- 内部 ----------------------------------------------------------

    def _drop(self, cache_id: str, *, key: tuple[str, str, str, str] | None = None) -> None:
        entry = self._entries.pop(cache_id, None)
        if entry is None:
            return
        self._exact_index.pop((entry.namespace, entry.tenant_id, entry.model, entry.fingerprint), None)

    def _notify_expired(self, n: int = 1) -> None:
        if self._on_expired is not None:
            self._on_expired(n)

    @property
    def size(self) -> int:
        return len(self._entries)
