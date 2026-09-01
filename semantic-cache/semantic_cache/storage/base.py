"""Cache Store 统一接口（设计说明书 §27）。

Demo 用 ``MemoryStore``，之后可平滑升级为：
  - Redis（精确缓存 + 额外索引）
  - PostgreSQL + pgvector（推荐，§28 ~ §31）
  - Milvus / Qdrant / FAISS

接口保持「语义化」：调用方只关心 exact get / insert / search / delete，
不关心底层是内存还是向量数据库。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.entry import CacheEntry, SearchResult


class CacheStore(ABC):
    @abstractmethod
    async def get_exact(
        self,
        *,
        namespace: str,
        tenant_id: str,
        model: str,
        fingerprint: str,
    ) -> CacheEntry | None:
        """按精确指纹取缓存（§9 / §49 第 2 步）。已过期条目视为不存在。"""
        raise NotImplementedError

    @abstractmethod
    async def insert(self, entry: CacheEntry) -> None:
        """写入一条缓存（§50 Cache Set）。"""
        raise NotImplementedError

    @abstractmethod
    async def search(
        self,
        query_vector: list[float],
        *,
        namespace: str,
        tenant_id: str,
        model: str,
        top_k: int,
    ) -> list[SearchResult]:
        """按向量相似度返回 Top-K 候选（§12 ~ §13）。

        实现必须保证（§31）：
          - 只返回 ``namespace/tenant_id/model`` 匹配的条目（租户隔离 §20）
          - 排除已过期条目（TTL §22）
          - 按相似度降序，最多 ``top_k`` 条
        """
        raise NotImplementedError

    @abstractmethod
    async def delete(self, cache_id: str) -> None:
        """按 cache_id 删除单条。"""
        raise NotImplementedError

    @abstractmethod
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
        """按条件批量删除（§24 Invalidation）。返回删除条数。"""
        raise NotImplementedError

    @abstractmethod
    async def count(
        self,
        *,
        namespace: str | None = None,
        tenant_id: str | None = None,
    ) -> int:
        """当前缓存条目数（用于 metrics gauge）。"""
        raise NotImplementedError

    async def close(self) -> None:
        """释放底层连接（内存版无操作，PG 版关闭连接池）。"""
