"""PostgresStore —— PostgreSQL + pgvector（设计说明书 §28 ~ §31）。

推荐的生产升级路径：MemoryStore -> PostgresStore，语义完全一致。
需要：
  - pip install semantic-cache[postgres]
  - 已应用 migrations/001_create_cache.sql（docker-compose 首次启动自动执行）

asyncpg 采用惰性导入/连接，未安装 asyncpg 或未配置 DATABASE_URL 时
不会影响其余模块的导入。
"""
from __future__ import annotations

import json
from typing import Any

from ..core.entry import CacheEntry, SearchResult
from ..search import pgvector as sql
from .base import CacheStore


def _vector_from_db(value: Any) -> list[float]:
    """asyncpg 把 vector 返回为 '[...]' 文本，转成 list[float]。"""
    if isinstance(value, list):
        return [float(x) for x in value]
    if isinstance(value, str):
        return [float(x) for x in value.strip("[]").split(",")] if value.strip("[]") else []
    raise TypeError(f"unexpected pgvector value: {type(value)}")


def _row_to_entry(row: Any) -> CacheEntry:
    return CacheEntry(
        cache_id=str(row["id"]),
        namespace=row["namespace"],
        tenant_id=row["tenant_id"],
        model=row["model"],
        fingerprint=row["fingerprint"],
        system_fingerprint=row["system_fingerprint"],
        prompt=row["prompt"],
        embedding=_vector_from_db(row["embedding"]),
        response=json.loads(row["response"]),
        temperature=row["temperature"],
        knowledge_version=row["knowledge_version"],
        agent_type=row["agent_type"],
        task_type=row["task_type"],
        context_version=row["context_version"],
        created_at=row["created_at"].timestamp() if row["created_at"] is not None else 0.0,
        expires_at=row["expires_at"].timestamp() if row["expires_at"] is not None else 0.0,
        hit_count=row["hit_count"] or 0,
    )


class PostgresStore(CacheStore):
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: Any = None

    async def _conn(self):
        if self._pool is None:
            import asyncpg  # 惰性导入：未安装 asyncpg 时不影响其余功能

            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ---- 精确缓存 ------------------------------------------------------

    async def get_exact(self, *, namespace: str, tenant_id: str, model: str, fingerprint: str) -> CacheEntry | None:
        _sql, _ = sql.get_exact_sql()
        pool = await self._conn()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_sql, namespace, tenant_id, model, fingerprint)
        return _row_to_entry(row) if row else None

    # ---- 写入 ----------------------------------------------------------

    async def insert(self, entry: CacheEntry) -> None:
        pool = await self._conn()
        params = [
            entry.cache_id,
            entry.namespace,
            entry.tenant_id,
            entry.model,
            entry.fingerprint,
            entry.system_fingerprint,
            entry.prompt,
            sql.vector_literal(entry.embedding),
            json.dumps(entry.response, ensure_ascii=False),
            entry.temperature,
            entry.knowledge_version,
            entry.agent_type,
            entry.task_type,
            entry.context_version,
            entry.created_at,
            entry.expires_at,
            entry.hit_count,
        ]
        async with pool.acquire() as conn:
            await conn.execute(sql.insert_sql(), *params)

    # ---- 向量检索（§31）------------------------------------------------

    async def search(
        self,
        query_vector: list[float],
        *,
        namespace: str,
        tenant_id: str,
        model: str,
        top_k: int,
    ) -> list[SearchResult]:
        _sql, params = sql.search_sql(namespace, tenant_id, model, top_k)
        params.append(sql.vector_literal(query_vector))
        pool = await self._conn()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_sql, *params)
        return [SearchResult(entry=_row_to_entry(r), similarity=float(r["similarity"])) for r in rows]

    # ---- 删除 / 失效 ---------------------------------------------------

    async def delete(self, cache_id: str) -> None:
        pool = await self._conn()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM semantic_cache WHERE id = $1::uuid", cache_id)

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
        _sql, params = sql.delete_many_sql(
            namespace=namespace,
            tenant_id=tenant_id,
            model=model,
            knowledge_version=knowledge_version,
            agent_type=agent_type,
            task_type=task_type,
        )
        pool = await self._conn()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_sql, *params)
        return len(rows)

    async def count(self, *, namespace: str | None = None, tenant_id: str | None = None) -> int:
        _sql, params = sql.count_sql(namespace, tenant_id)
        pool = await self._conn()
        async with pool.acquire() as conn:
            return int((await conn.fetchval(_sql, *params)) or 0)
