"""pgvector 检索 SQL（设计说明书 §30 ~ §31）。

要点（§31）：不要直接 ``ORDER BY embedding <=> query LIMIT k``。
必须先按 ``namespace/tenant_id/model`` 过滤、并排除 ``expires_at <= NOW()``，
再做余弦距离排序 —— Tenant Isolation + TTL + Similarity 同时成立。
"""
from __future__ import annotations

_COLUMNS = (
    "id, namespace, tenant_id, model, fingerprint, system_fingerprint, "
    "prompt, embedding, response, temperature, knowledge_version, "
    "agent_type, task_type, context_version, created_at, expires_at, hit_count"
)

# 向量以文本形式传入，如 "[0.1,-0.2,...]"，用 ::vector 显式转换
def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def get_exact_sql() -> tuple[str, list]:
    return (
        f"""
        SELECT {_COLUMNS}
        FROM semantic_cache
        WHERE namespace = $1 AND tenant_id = $2 AND model = $3 AND fingerprint = $4
          AND expires_at > NOW()
        """,
        [],
    )


def search_sql(namespace: str, tenant_id: str, model: str, top_k: int) -> tuple[str, list]:
    """检索 Top-K。查询向量作为第 5 个参数追加（$5）。"""
    sql = f"""
        SELECT {_COLUMNS}, 1 - (embedding <=> $5::vector) AS similarity
        FROM semantic_cache
        WHERE namespace = $1 AND tenant_id = $2 AND model = $3
          AND expires_at > NOW()
        ORDER BY embedding <=> $5::vector
        LIMIT $4
    """
    return sql, [namespace, tenant_id, model, top_k]


def insert_sql() -> str:
    return f"""
        INSERT INTO semantic_cache (
            id, namespace, tenant_id, model, fingerprint, system_fingerprint,
            prompt, embedding, response, temperature, knowledge_version,
            agent_type, task_type, context_version, created_at, expires_at, hit_count
        ) VALUES (
            $1::uuid, $2, $3, $4, $5, $6, $7, $8::vector, $9::jsonb, $10, $11, $12, $13, $14, $15, $16, $17
        )
        ON CONFLICT (id) DO NOTHING
    """


def delete_many_sql(
    *,
    namespace: str | None = None,
    tenant_id: str | None = None,
    model: str | None = None,
    knowledge_version: str | None = None,
    agent_type: str | None = None,
    task_type: str | None = None,
) -> tuple[str, list]:
    clauses: list[str] = []
    params: list[object] = []

    def add(column: str, value: object) -> None:
        params.append(value)
        clauses.append(f"{column} = ${len(params)}")

    if namespace is not None:
        add("namespace", namespace)
    if tenant_id is not None:
        add("tenant_id", tenant_id)
    if model is not None:
        add("model", model)
    if knowledge_version is not None:
        add("knowledge_version", knowledge_version)
    if agent_type is not None:
        add("agent_type", agent_type)
    if task_type is not None:
        add("task_type", task_type)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return f"DELETE FROM semantic_cache{where} RETURNING id", params


def count_sql(namespace: str | None, tenant_id: str | None) -> tuple[str, list]:
    clauses: list[str] = []
    params: list[object] = []

    def add(column: str, value: object) -> None:
        params.append(value)
        clauses.append(f"{column} = ${len(params)}")

    if namespace is not None:
        add("namespace", namespace)
    if tenant_id is not None:
        add("tenant_id", tenant_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return f"SELECT COUNT(*) FROM semantic_cache{where}", params
